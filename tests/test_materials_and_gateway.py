"""Stage 03a tests: materials loader, crop mapping, ZCode multimodal gateway.

All visual inputs are synthetic images/PDFs generated in tmp_path; the model
side is httpx.MockTransport (clearly separate from real recognition per
AGENTS.md).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import struct
import zlib
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from conftest import make_task, pass_batch
from o2_audit.domain.models import (
    AllowedAction,
    EntityKind,
    ExecutionError,
    MaterialRef,
    NeedEvidence,
    Observation,
    ObservationSource,
    ObservationStatus,
)
from o2_audit.materials.loader import (
    MAX_IMAGE_PIXELS,
    _decode_image_bytes,
    _validate_page_index,
    crop_region,
    encode_image_base64,
    load_material,
    map_crop_bbox_to_source,
)
from o2_audit.model_gateway.cache import ExtractionCache, cache_key
from o2_audit.model_gateway.client import MessagesClient, endpoint_url
from o2_audit.model_gateway.config import ModelConfig, load_model_config
from o2_audit.model_gateway.gateway import ZCodeObservationGateway
from o2_audit.model_gateway.parser import (
    extract_json_text,
    parse_annotation,
    parse_observation_batch,
)
from o2_audit.model_gateway.prompts import (
    annotation_system_prompt,
    build_annotation_prompt,
    build_observation_prompt,
    observation_system_prompt,
)
from o2_audit.protocol.checks import prediction_semantic_errors
from o2_audit.runtime.controller import AuditController
from o2_audit.runtime.gateway import ObservationBatch
from o2_audit.rules.b001.plan import plan_3_0
from o2_audit.storage.store import TaskStore
from o2_audit.operators.text import annotation_nfkc_strip

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------


def make_png_bytes(size=(64, 48), color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_jpeg_bytes(size=(64, 48), color=(30, 200, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def make_pdf_bytes(pages=3, size=(200, 150)) -> bytes:
    imgs = [Image.new("RGB", size, (i * 60, 100, 180)) for i in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def fake_huge_png_bytes(w=20000, h=20000) -> bytes:
    """Minimal PNG whose IHDR declares huge dimensions but whose data is tiny
    (decompression-bomb shape: small file, astronomically many pixels)."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def write_materials(tmp_path: Path, ids=("m1", "m2", "m3"), subdir="assets") -> None:
    """Real PNG files matching the conftest make_task material paths."""
    for i, mid in enumerate(ids):
        folder = tmp_path / subdir
        folder.mkdir(exist_ok=True)
        (folder / f"{mid}.png").write_bytes(
            make_png_bytes(color=((i * 70) % 256, 120, 200)))


def mref(mid: str, role: str, rel: str, media="image/png", page=1) -> MaterialRef:
    return MaterialRef(material_id=mid, role=role, relative_path=rel,
                       media_type=media, page_index=page)


def gap_all(material_ids: list[str]) -> NeedEvidence:
    return NeedEvidence(
        entity_kind=EntityKind.LABEL,
        predicate="model",
        candidate_material_ids=list(material_ids),
        allowed_actions=[AllowedAction.INSPECT_MATERIAL],
        reason="test_gap",
    )


def ok_response(text: str, input_tokens=42, output_tokens=7) -> httpx.Response:
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def model_output_json(batch: ObservationBatch, cited_ids) -> str:
    obs = []
    for o in batch.observations:
        if o.source is not None and o.source.material_id in set(cited_ids):
            d = json.loads(o.model_dump_json(exclude_none=True))
            if "raw_value" not in d and o.normalized_value is not None:
                d["raw_value"] = o.normalized_value
            d.pop("normalized_value", None)
            obs.append(d)
    ents = [json.loads(e.model_dump_json()) for e in batch.entities]
    return json.dumps({"entities": ents, "observations": obs}, ensure_ascii=False)


def make_mock_gateway(
    tmp_path: Path,
    batch: ObservationBatch | None = None,
    *,
    store: TaskStore | None = None,
    statuses=None,
    fixed_output: str | None = None,
):
    """Gateway over a MockTransport; ``calls`` counts HTTP requests."""
    calls = {"count": 0, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        calls["requests"].append(request)
        if statuses is not None:
            status = statuses.pop(0) if statuses else 500
            if status == "timeout":
                raise httpx.ReadTimeout("simulated slow upstream", request=request)
            if status == "connect_error":
                raise httpx.ConnectError("simulated network failure", request=request)
            if status != 200:
                return httpx.Response(status, text="upstream exploded")
        body = json.loads(request.content.decode("utf-8"))
        text = body["messages"][0]["content"][0]["text"]
        if fixed_output is not None:
            return ok_response(fixed_output)
        cited = re.findall(r'"material_id":\s*"([^"]+)"', text)
        return ok_response(model_output_json(batch or ObservationBatch(), cited))

    config = ModelConfig(max_retries=1, retry_backoff_seconds=0.0)
    client = MessagesClient(config, transport=httpx.MockTransport(handler))
    cache = ExtractionCache(store) if store is not None else None
    gateway = ZCodeObservationGateway(client, data_root=tmp_path, cache=cache)
    return gateway, calls


# ---------------------------------------------------------------------------
# materials loader
# ---------------------------------------------------------------------------


class TestMaterialsLoader:
    def test_load_png(self, tmp_path):
        data = make_png_bytes(size=(64, 48))
        (tmp_path / "m1.png").write_bytes(data)
        loaded = load_material(tmp_path, mref("m1", "tag_photos", "m1.png"))
        assert (loaded.source_width, loaded.source_height) == (64, 48)
        assert loaded.content_sha256 == hashlib.sha256(data).hexdigest()
        assert loaded.page_index == 1
        assert loaded.pdf_page_count is None
        assert loaded.image.size == (64, 48)

    def test_load_jpeg(self, tmp_path):
        (tmp_path / "m1.jpg").write_bytes(make_jpeg_bytes())
        loaded = load_material(tmp_path, mref("m1", "tag_photos", "m1.jpg", "image/jpeg"))
        assert loaded.image.size == (64, 48)

    def test_webp_decodes_via_low_level_loader(self):
        buf = io.BytesIO()
        Image.new("RGB", (40, 20), (5, 5, 5)).save(buf, "WEBP")
        img = _decode_image_bytes(buf.getvalue(), "image/webp", MAX_IMAGE_PIXELS)
        assert img.size == (40, 20)

    def test_declared_format_mismatch_rejected(self, tmp_path):
        (tmp_path / "m1.png").write_bytes(make_png_bytes())
        # declared JPEG, actually a PNG file
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "m1.png", "image/jpeg"))
        assert e.value.error_code == "material_format_mismatch"

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "nope.png"))
        assert e.value.error_code == "material_not_found"

    def test_traversal_rejected(self, tmp_path):
        (tmp_path.parent / "outside.png").write_bytes(make_png_bytes())
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "../outside.png"))
        assert e.value.error_code == "material_path_invalid"

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "C:/Windows/win.ini"))
        assert e.value.error_code == "material_path_invalid"

    def test_symlink_rejected(self, tmp_path):
        target = tmp_path / "real.png"
        target.write_bytes(make_png_bytes())
        link = tmp_path / "link.png"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("symlink creation not permitted on this platform")
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "link.png"))
        assert e.value.error_code == "material_path_invalid"

    def test_oversized_file_rejected(self, tmp_path):
        (tmp_path / "big.png").write_bytes(make_png_bytes(size=(200, 200)))
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "big.png"), max_bytes=10)
        assert e.value.error_code == "material_too_large"

    def test_decompression_bomb_rejected(self, tmp_path):
        (tmp_path / "bomb.png").write_bytes(fake_huge_png_bytes())
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("m1", "tag_photos", "bomb.png"))
        assert e.value.error_code == "material_too_many_pixels"

    def test_pdf_page_rendering_and_page_count(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(make_pdf_bytes(pages=3))
        loaded = load_material(tmp_path, mref("d1", "certificate_pages", "doc.pdf",
                                              "application/pdf", page=2))
        assert loaded.page_index == 2  # exact 1-based SOURCE page rendered
        assert loaded.pdf_page_count == 3
        assert loaded.image.size[0] > 0 and loaded.image.size[1] > 0

    def test_pdf_page_out_of_range_rejected(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(make_pdf_bytes(pages=3))
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("d1", "certificate_pages", "doc.pdf",
                                         "application/pdf", page=4))
        assert e.value.error_code == "invalid_page_index"

    def test_pdf_page_zero_rejected(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(make_pdf_bytes(pages=3))
        with pytest.raises(ExecutionError) as e:
            load_material(tmp_path, mref("d1", "certificate_pages", "doc.pdf",
                                         "application/pdf", page=0))
        assert e.value.error_code == "invalid_page_index"

    def test_bool_page_index_rejected(self):
        with pytest.raises(ValidationError):
            mref("x", "tag_photos", "x.png", page=True)  # bool is not an int
        with pytest.raises(ExecutionError) as e:
            _validate_page_index(True)
        assert e.value.error_code == "invalid_page_index"

    def test_downscale_preserves_source_dimensions(self, tmp_path):
        (tmp_path / "big.png").write_bytes(make_png_bytes(size=(1000, 600)))
        loaded = load_material(tmp_path, mref("m1", "tag_photos", "big.png"),
                               max_edge=200)
        # transmitted view downscaled...
        assert loaded.image.size == (200, 120)
        # ...while the EXACT full-source dimensions are preserved (normalized
        # [0,1] coordinates keep mapping to the original source image).
        assert (loaded.source_width, loaded.source_height) == (1000, 600)

    def test_crop_region_pixel_accuracy(self):
        img = Image.new("RGB", (100, 80), (255, 0, 0))
        cropped = crop_region(img, (0.25, 0.5, 0.75, 1.0))
        assert cropped.size == (50, 40)

    def test_encode_image_base64_roundtrip(self):
        img = Image.new("RGB", (10, 10), (1, 2, 3))
        payload, mime = encode_image_base64(img, "PNG")
        assert mime == "image/png"
        decoded = base64.b64decode(payload)
        assert decoded.startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# crop inverse coordinate mapping
# ---------------------------------------------------------------------------


class TestCropInverseMapping:
    CROP = (0.25, 0.5, 0.75, 1.0)

    def test_identity_sub_bbox_maps_to_crop(self):
        assert map_crop_bbox_to_source(self.CROP, (0.0, 0.0, 1.0, 1.0)) == self.CROP

    def test_exact_partial_mapping(self):
        # sub (0,0,0.5,0.5) of crop (0.25,0.5,0.75,1.0)
        assert map_crop_bbox_to_source(self.CROP, (0.0, 0.0, 0.5, 0.5)) == \
            (0.25, 0.5, 0.5, 0.75)

    def test_center_mapping(self):
        src = map_crop_bbox_to_source(self.CROP, (0.25, 0.25, 0.75, 0.75))
        assert src == pytest.approx((0.375, 0.625, 0.625, 0.875))

    def test_tiny_epsilon_is_clamped(self):
        src = map_crop_bbox_to_source(
            self.CROP, (-1e-9, -1e-9, 1.0 + 1e-9, 1.0))
        assert src == pytest.approx(self.CROP)

    @pytest.mark.parametrize("bad", [
        (0.5, 0.5, 0.2, 0.2),      # inverted
        (0.0, 0.0, 1.5, 1.0),      # out of range
        (-0.5, 0.0, 0.5, 0.5),     # negative beyond epsilon
        (0.0, 0.0, float("nan"), 1.0),  # non-finite
        (0.0, 0.0, True, 1.0),     # bool coordinate
        (0.1, 0.2),                # wrong arity
    ])
    def test_invalid_sub_bbox_rejected(self, bad):
        with pytest.raises(ExecutionError) as e:
            map_crop_bbox_to_source(self.CROP, bad)
        assert e.value.error_code == "invalid_bbox"

    @pytest.mark.parametrize("bad_crop", [
        (0.5, 0.5, 0.2, 0.2), (0.0, 0.0, 2.0, 1.0),
        (0.0, 0.0, float("inf"), 1.0),
    ])
    def test_invalid_crop_bbox_rejected(self, bad_crop):
        with pytest.raises(ExecutionError) as e:
            map_crop_bbox_to_source(bad_crop, (0.0, 0.0, 1.0, 1.0))
        assert e.value.error_code == "invalid_bbox"


# ---------------------------------------------------------------------------
# model config
# ---------------------------------------------------------------------------


@pytest.fixture()
def clean_env(monkeypatch):
    for name in ("O2_BASE_URL", "O2_API_KEY",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestModelConfig:
    def test_missing_env_raises_model_not_configured(self, clean_env):
        config = ModelConfig()
        with pytest.raises(ExecutionError) as e:
            config.resolve_credentials()
        assert e.value.error_code == "model_not_configured"

    def test_blank_env_raises_model_not_configured(self, clean_env):
        clean_env.setenv("O2_BASE_URL", "   ")
        clean_env.setenv("O2_API_KEY", "")
        with pytest.raises(ExecutionError) as e:
            ModelConfig().resolve_credentials()
        assert e.value.error_code == "model_not_configured"

    def test_primary_env_preferred_fallback_used(self, clean_env):
        clean_env.setenv("ANTHROPIC_BASE_URL", "http://fallback.test")
        clean_env.setenv("ANTHROPIC_AUTH_TOKEN", "fallback-token")
        base, key = ModelConfig().resolve_credentials()
        assert base == "http://fallback.test" and key == "fallback-token"
        clean_env.setenv("O2_BASE_URL", "http://primary.test")
        clean_env.setenv("O2_API_KEY", "primary-token")
        base, key = ModelConfig().resolve_credentials()
        assert base == "http://primary.test" and key == "primary-token"

    def test_config_serialization_never_contains_secret(self, clean_env):
        clean_env.setenv("O2_API_KEY", "sekret-token-do-not-leak")
        clean_env.setenv("O2_BASE_URL", "http://sekret-host.example")
        dump = json.dumps(ModelConfig().safe_dump())
        assert "sekret-token-do-not-leak" not in dump
        assert "sekret-host.example" not in dump
        assert dump.count("O2_API_KEY") == 1  # the env NAME, never a value

    def test_example_config_is_secret_free_and_loads(self):
        path = REPO_ROOT / "configs" / "model_config.example.json"
        config = load_model_config(path)
        assert config.model == "zcode/GLM-5.3-Flash"
        assert config.api_key_env == "O2_API_KEY"
        dump = json.dumps(config.safe_dump())
        for forbidden in ("Bearer", "sk-", "token="):
            assert forbidden not in dump


# ---------------------------------------------------------------------------
# MessagesClient over httpx.MockTransport
# ---------------------------------------------------------------------------


@pytest.fixture()
def gw_env(clean_env):
    clean_env.setenv("O2_BASE_URL", "http://gw.test")
    clean_env.setenv("O2_API_KEY", "tok-abc123-secret")
    return clean_env


def make_client(handler, **config_overrides) -> MessagesClient:
    params = {"max_retries": 1, "retry_backoff_seconds": 0.0}
    params.update(config_overrides)
    config = ModelConfig(**params)
    return MessagesClient(config, transport=httpx.MockTransport(handler))


class TestMessagesClient:
    def test_request_structure_and_headers(self, gw_env):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return ok_response("hi")

        client = make_client(handler)
        payload, mime = encode_image_base64(Image.new("RGB", (8, 8)), "PNG")
        reply = client.messages("SYS", "USER TASK", [(payload, mime)])
        assert reply.text == "hi"
        assert captured["url"].rstrip("/").endswith("http://gw.test/v1/messages".replace("http://gw.test", ""))
        assert captured["url"] == "http://gw.test/v1/messages"
        assert captured["headers"]["content-type"] == "application/json"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        assert captured["headers"]["x-api-key"] == "tok-abc123-secret"
        assert captured["headers"]["authorization"] == "Bearer tok-abc123-secret"
        body = captured["body"]
        assert body["model"] == "zcode/GLM-5.3-Flash"
        assert body["max_tokens"] == 4096
        assert body["temperature"] == 0.0
        assert body["system"] == "SYS"
        blocks = body["messages"][0]["content"]
        assert body["messages"][0]["role"] == "user"
        assert blocks[0] == {"type": "text", "text": "USER TASK"}
        image_block = blocks[1]
        assert image_block["type"] == "image"
        assert image_block["source"]["media_type"] == "image/png"
        assert base64.b64decode(image_block["source"]["data"]).startswith(b"\x89PNG")

    @pytest.mark.parametrize("base", ["http://gw.test/", "http://gw.test/v1",
                                      "http://gw.test/v1/"])
    def test_base_url_variants_hit_v1_messages(self, gw_env, base):
        gw_env.setenv("O2_BASE_URL", base)
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return ok_response("ok")

        make_client(handler).messages("S", "T")
        assert seen[-1] == "http://gw.test/v1/messages"
        assert endpoint_url(base) == "http://gw.test/v1/messages"

    def test_transient_retry_then_success(self, gw_env):
        statuses = [429, 500]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(statuses.pop(0), text="hiccup") if statuses \
                else ok_response("recovered")

        client = make_client(handler, max_retries=2)
        reply = client.messages("S", "T")
        assert reply.text == "recovered"
        assert client.usage.calls == 3
        assert client.usage.retries == 2

    def test_persistent_500_raises_gateway_http_error(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="always broken")

        client = make_client(handler, max_retries=1)
        with pytest.raises(ExecutionError) as e:
            client.messages("S", "T")
        assert e.value.error_code == "gateway_http_error"
        assert client.usage.calls == 2

    def test_timeout_raises_gateway_timeout(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated timeout", request=request)

        client = make_client(handler, max_retries=0)
        with pytest.raises(ExecutionError) as e:
            client.messages("S", "T")
        assert e.value.error_code == "gateway_timeout"

    def test_network_error_raises_gateway_network_error(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        client = make_client(handler, max_retries=0)
        with pytest.raises(ExecutionError) as e:
            client.messages("S", "T")
        assert e.value.error_code == "gateway_network_error"

    def test_malformed_envelope_raises_parse_error(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="this is not json {")

        with pytest.raises(ExecutionError) as e:
            make_client(handler).messages("S", "T")
        assert e.value.error_code == "gateway_parse_error"

    def test_http_400_raises_http_error(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request shape")

        with pytest.raises(ExecutionError) as e:
            make_client(handler).messages("S", "T")
        assert e.value.error_code == "gateway_http_error"

    def test_truncated_response_raises_incomplete(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "{\"entities\": ["}],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        with pytest.raises(ExecutionError) as e:
            make_client(handler).messages("S", "T")
        assert e.value.error_code == "gateway_incomplete_response"

    def test_empty_content_raises_incomplete(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [], "stop_reason": "end_turn"})

        with pytest.raises(ExecutionError) as e:
            make_client(handler).messages("S", "T")
        assert e.value.error_code == "gateway_incomplete_response"

    def test_refusal_raises_parse_error(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "content": [{"type": "refusal", "refusal": "no"}],
                "stop_reason": "refusal",
            })

        with pytest.raises(ExecutionError) as e:
            make_client(handler).messages("S", "T")
        assert e.value.error_code == "gateway_parse_error"

    def test_token_never_leaks_into_errors(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="server error detail")

        client = make_client(handler, max_retries=0)
        with pytest.raises(ExecutionError) as e:
            client.messages("S", "T")
        assert "tok-abc123-secret" not in str(e.value)
        assert "tok-abc123-secret" not in repr(e.value)
        assert "tok-abc123-secret" not in e.value.message

    def test_usage_tracking(self, gw_env):
        def handler(request: httpx.Request) -> httpx.Response:
            return ok_response("ok", input_tokens=11, output_tokens=3)

        client = make_client(handler)
        client.messages("S", "T")
        summary = client.usage.summary()
        assert summary["input_tokens"] == 11
        assert summary["output_tokens"] == 3
        assert summary["calls"] == 1


# ---------------------------------------------------------------------------
# strict parser
# ---------------------------------------------------------------------------


ALLOWED_ONE = {"m1": mref("m1", "tag_photos", "a.png")}
ALLOWED_THREE = {
    "m1": mref("m1", "package_photos", "a.png"),
    "m2": mref("m2", "tag_photos", "b.png"),
    "m3": mref("m3", "certificate_pages", "c.png"),
}


def obs_json(entity_id, predicate, status, raw=None, scope=False, source=None, **extra) -> dict:
    out = {"entity_id": entity_id, "predicate": predicate, "status": status,
           "raw_value": raw, "scope_complete": scope}
    if source is not None:
        out["source"] = source
    out.update(extra)
    return out


class TestParser:
    def test_fenced_and_plain_json(self):
        assert extract_json_text('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert extract_json_text('{"a": 1}') == '{"a": 1}'
        assert extract_json_text('说明 {"a": 1} 结尾') == '{"a": 1}'

    def test_valid_batch_with_fence(self):
        text = (
            "以下是观察结果：\n```json\n"
            + json.dumps({
                "entities": [{"entity_id": "L1", "kind": "label"}],
                "observations": [obs_json("L1", "void_marked", "readable", False,
                                          source={"material_id": "m1", "page_index": 1})],
            })
            + "\n```"
        )
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert report.clean
        assert [e.entity_id for e in batch.entities] == ["L1"]
        assert batch.observations[0].normalized_value is False

    def test_model_emitted_label_field_never_accepted(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [
                obs_json("L1", "void_marked", "readable", False,
                         label="pass",  # forbidden compliance verdict field
                         source={"material_id": "m1"}),
            ],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert batch.observations == []  # dropped, never honored
        assert not report.clean
        assert "forbid" in report.dropped[0]["reason"]

    def test_unknown_material_id_dropped(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "model", "readable", "LED-100",
                                      source={"material_id": "m99", "page_index": 1})],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert batch.observations == []
        assert "unknown material_id" in report.dropped[0]["reason"]

    def test_omitted_material_binds_to_single_material(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "model", "readable", "LED-100")],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert report.clean
        assert batch.observations[0].source.material_id == "m1"
        assert batch.observations[0].source.page_index == 1

    def test_mismatched_page_index_corrected(self):
        text = json.dumps({
            "entities": [{"entity_id": "C1", "kind": "certificate"}],
            "observations": [obs_json("C1", "document_number", "readable", "DOC-1",
                                      source={"material_id": "m1", "page_index": 7})],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert batch.observations[0].source.page_index == 1  # legal source page
        assert any("corrected" in c for c in report.corrections)

    def test_bad_bbox_discarded_not_fabricated(self):
        for bad_bbox in ([0.5, 0.5, 0.2, 0.2], [0.0, 0.0, 1.5, 1.0], [0.1, 0.2]):
            text = json.dumps({
                "entities": [{"entity_id": "L1", "kind": "label"}],
                "observations": [obs_json("L1", "model", "readable", "LED-100",
                                          source={"material_id": "m1", "bbox": bad_bbox})],
            })
            batch, report = parse_observation_batch(text, ALLOWED_ONE)
            assert batch.observations[0].source.bbox is None
            assert any("bbox" in d["reason"] for d in report.dropped)

    def test_nonfinite_json_rejected_entirely(self):
        text = ('{"entities": [], "observations": [{"entity_id": "L1", '
                '"predicate": "model", "status": "readable", '
                '"raw_value": NaN}]}')
        with pytest.raises(ExecutionError) as e:
            parse_observation_batch(text, ALLOWED_ONE)
        assert e.value.error_code == "gateway_parse_error"

    def test_confirmed_missing_downgraded_without_complete_scope(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "model", "confirmed_missing",
                                      None, scope=False, source={"material_id": "m1"})],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        obs = batch.observations[0]
        assert obs.status.value == "unreadable"  # false absence cannot slip through
        assert obs.raw_value is None and obs.normalized_value is None
        assert obs.scope_complete is False
        assert any("downgraded" in c for c in report.corrections)

    def test_confirmed_missing_with_complete_scope_preserved(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "model", "confirmed_missing",
                                      None, scope=True, source={"material_id": "m1"})],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert batch.observations[0].status.value == "confirmed_missing"
        assert batch.observations[0].scope_complete is True

    def test_raw_verbatim_and_shared_operator_normalization(self):
        raw = " LED－100 "  # full-width characters preserved verbatim in raw_value
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "model", "readable", raw,
                                      source={"material_id": "m1"})],
        })
        batch, _ = parse_observation_batch(text, ALLOWED_ONE)
        obs = batch.observations[0]
        assert obs.raw_value == raw  # transcription untouched
        assert obs.normalized_value == "led100"  # shared model normalization

    def test_separate_entities_and_per_entity_void_marked_preserved(self):
        text = json.dumps({
            "entities": [{"entity_id": "LA", "kind": "label"},
                         {"entity_id": "LB", "kind": "label"}],
            "observations": [
                obs_json("LA", "void_marked", "readable", True,
                         source={"material_id": "m1"}),
                obs_json("LB", "void_marked", "readable", False,
                         source={"material_id": "m1"}),
            ],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert report.clean
        voids = {o.entity_id: o.normalized_value for o in batch.observations}
        assert voids == {"LA": True, "LB": False}  # never merged, never global

    def test_unknown_predicate_entity_status_dropped(self):
        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [
                obs_json("L1", "is_this_product_compliant", "readable", "yes",
                         source={"material_id": "m1"}),  # free-form predicate
                obs_json("LX", "model", "readable", "X",
                         source={"material_id": "m1"}),  # unknown entity
                obs_json("L1", "model", "totally_fine", "X",
                         source={"material_id": "m1"}),  # unknown status
            ],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert batch.observations == []
        assert len(report.dropped) == 3

    def test_structurally_broken_output_raises(self):
        for text in ('{"entities": 5, "observations": []}', '[1,2,3]', 'no json here }'):
            with pytest.raises(ExecutionError) as e:
                parse_observation_batch(text, ALLOWED_ONE)
            assert e.value.error_code == "gateway_parse_error"

    def test_model_list_predicate_normalizes_elements(self):
        text = json.dumps({
            "entities": [{"entity_id": "C1", "kind": "certificate"}],
            "observations": [obs_json("C1", "covered_models", "readable",
                                      ["EDU-753", " LED－100 "],
                                      source={"material_id": "m1"})],
        })
        batch, report = parse_observation_batch(text, ALLOWED_ONE)
        assert report.clean
        obs = batch.observations[0]
        assert obs.raw_value == ["EDU-753", " LED－100 "]
        assert obs.normalized_value == ["edu753", "led100"]

    def test_crop_bbox_remapper_applied(self):
        def remap(material_id, bbox):
            return map_crop_bbox_to_source((0.0, 0.0, 0.5, 0.5), bbox)

        text = json.dumps({
            "entities": [{"entity_id": "L1", "kind": "label"}],
            "observations": [obs_json("L1", "void_marked", "readable", False,
                                      source={"material_id": "m1",
                                              "bbox": [0.1, 0.1, 0.3, 0.3]})],
        })
        batch, _ = parse_observation_batch(text, ALLOWED_ONE, bbox_remapper=remap)
        assert batch.observations[0].source.bbox == pytest.approx((0.05, 0.05, 0.15, 0.15))

    # -- annotation parsing (Stage 03b) -------------------------------------

    def test_annotation_value(self):
        text = json.dumps({"text": "QLAB－123456", "status": "value",
                           "bbox": [0.2, 0.2, 0.6, 0.6], "material_id": "m1"})
        result, report = parse_annotation(text, ALLOWED_ONE)
        assert report.clean
        assert result.text == annotation_nfkc_strip("QLAB－123456") == "QLAB-123456"
        assert result.status == "value"
        assert result.source.material_id == "m1"
        assert result.bbox == (0.2, 0.2, 0.6, 0.6)

    def test_annotation_statuses(self):
        for status in ("unreadable", "invalid_value"):
            text = json.dumps({"text": None, "status": status})
            result, _ = parse_annotation(text, ALLOWED_ONE)
            assert result.status == status
            assert result.bbox is None
            assert result.source is not None and result.source.material_id == "m1"

    def test_annotation_unreadable_forces_null_text(self):
        text = json.dumps({"text": "should-be-cleared", "status": "unreadable",
                           "bbox": [0.1, 0.1, 0.4, 0.3], "material_id": "m1"})
        result, report = parse_annotation(text, ALLOWED_ONE)
        assert result.status == "unreadable"
        assert result.text is None
        assert report.corrections

    def test_annotation_bad_status_rejected(self):
        with pytest.raises(ExecutionError) as e:
            parse_annotation(json.dumps({"text": "x", "status": "pass"}), ALLOWED_ONE)
        assert e.value.error_code == "gateway_parse_error"

    def test_annotation_unknown_material_rejected(self):
        text = json.dumps({"text": "x", "status": "value", "material_id": "m99"})
        with pytest.raises(ExecutionError, match="unknown material_id"):
            parse_annotation(text, ALLOWED_ONE)

    def test_annotation_invalid_bbox_not_fabricated(self):
        text = json.dumps({"text": "x", "status": "value",
                           "bbox": [0.9, 0.9, 0.1, 0.1], "material_id": "m1"})
        result, report = parse_annotation(text, ALLOWED_ONE)
        assert result.bbox is None
        assert report.dropped


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------


class TestPrompts:
    def test_observation_prompt_forbids_labels(self):
        system = observation_system_prompt()
        assert "pass/reject/ambiguous" in system
        assert "不可信数据" in system  # material text is untrusted data
        assert "不要凭照片猜测物理尺寸" in system
        assert "void_marked" in system  # vocabulary present
        assert "entity_id" in system

    def test_observation_user_prompt_lists_materials(self):
        gap = gap_all(["m1", "m2"])
        prompt = build_observation_prompt(gap, [ALLOWED_THREE["m1"], ALLOWED_THREE["m2"]])
        assert '"material_id": "m1"' in prompt
        assert '"material_id": "m2"' in prompt
        assert "model" in prompt

    def test_prompt_fingerprint_excludes_material_ids(self):
        gap = gap_all(["m1"])
        a = [mref("m1", "tag_photos", "a.png")]
        b = [mref("OTHER-ID", "tag_photos", "a.png")]
        assert (observation_system_prompt() == observation_system_prompt())
        from o2_audit.model_gateway.prompts import prompt_fingerprint
        assert prompt_fingerprint(gap, a, "S") == prompt_fingerprint(gap, b, "S")

    def test_annotation_prompt_contract(self):
        prompt = build_annotation_prompt("材料编号", [ALLOWED_ONE["m1"]])
        assert "NFKC" in prompt
        assert "value" in prompt and "unreadable" in prompt and "invalid_value" in prompt
        assert annotation_system_prompt().startswith("你是一个严格的目标标注助手")


# ---------------------------------------------------------------------------
# gateway: observe, crops, cache
# ---------------------------------------------------------------------------


class TestZCodeObservationGateway:
    def test_observe_returns_parsed_batch(self, tmp_path, gw_env):
        write_materials(tmp_path)
        batch = pass_batch()
        gateway, calls = make_mock_gateway(tmp_path, batch)
        gateway.bind_task(make_task())
        result = gateway.observe(gap_all(["m1", "m2", "m3"]))
        assert calls["count"] == 1
        assert {e.entity_id for e in result.entities} == {"L1", "C1"}
        assert len(result.observations) == len(batch.observations)
        assert any(o.predicate == "void_marked" and o.entity_id == "L1"
                   for o in result.observations)
        usage = gateway.usage_summary()
        assert usage["calls"] == 1 and usage["input_tokens"] == 42

    def test_observe_crop_maps_bbox_to_source(self, tmp_path, gw_env):
        write_materials(tmp_path)
        # one observation with a crop-local bbox on m1
        from o2_audit.domain.models import Entity
        batch = ObservationBatch(
            entities=[Entity(entity_id="L1", kind=EntityKind.LABEL)],
            observations=[Observation(
                entity_id="L1", predicate="void_marked",
                status=ObservationStatus.READABLE,
                raw_value=False, normalized_value=False,
                source=ObservationSource(material_id="m1", page_index=1,
                                         bbox=(0.1, 0.1, 0.3, 0.3)),
            )],
        )
        gateway, calls = make_mock_gateway(tmp_path, batch)
        gateway.bind_task(make_task())
        result = gateway.observe(gap_all(["m1", "m2", "m3"]),
                                 crops={"m1": (0.0, 0.0, 0.5, 0.5)})
        assert calls["count"] == 1
        assert result.observations[0].source.bbox == pytest.approx((0.05, 0.05, 0.15, 0.15))

    def test_missing_material_raises_execution_error(self, tmp_path, gw_env):
        write_materials(tmp_path, ids=("m1", "m2"))  # m3.png missing
        gateway, _ = make_mock_gateway(tmp_path)
        gateway.bind_task(make_task())
        with pytest.raises(ExecutionError) as e:
            gateway.observe(gap_all(["m1", "m2", "m3"]))
        assert e.value.error_code == "material_not_found"

    def test_observe_without_binding_raises(self, tmp_path, gw_env):
        gateway, _ = make_mock_gateway(tmp_path)
        with pytest.raises(ExecutionError) as e:
            gateway.observe(gap_all(["m1"]))
        assert e.value.error_code == "gateway_not_bound"

    def test_cache_hit_avoids_second_call_and_rebinds_material_ids(
            self, tmp_path, gw_env):
        write_materials(tmp_path)  # unit 1: m1..m3
        # unit 2: different material IDs, byte-identical content
        write_materials(tmp_path, ids=("n1", "n2", "n3"), subdir="assets2")
        for src, dst in zip(("m1", "m2", "m3"), ("n1", "n2", "n3")):
            (tmp_path / "assets2" / f"{dst}.png").write_bytes(
                (tmp_path / "assets" / f"{src}.png").read_bytes())

        store = TaskStore(tmp_path / "state.sqlite3")
        gateway, calls = make_mock_gateway(tmp_path, pass_batch(), store=store)
        request1 = gap_all(["m1", "m2", "m3"])

        gateway.bind_task(make_task(), plan_identity="B001:3.0")
        first = gateway.observe(request1)
        assert calls["count"] == 1
        assert {o.source.material_id for o in first.observations} <= {"m1", "m2", "m3"}

        task2 = make_task(
            audit_unit_id="u-2",
            materials=[mref("n1", "package_photos", "assets2/n1.png"),
                       mref("n2", "tag_photos", "assets2/n2.png"),
                       mref("n3", "certificate_pages", "assets2/n3.png")],
        )
        gateway.bind_task(task2, plan_identity="B001:3.0")
        second = gateway.observe(gap_all(["n1", "n2", "n3"]))

        assert calls["count"] == 1  # content-addressed cache hit: NO second HTTP call
        assert gateway.usage_summary()["cache_hits"] == 1
        # CRITICAL: cached observations are rebound to unit 2's legal IDs —
        # unit 1's material IDs never leak.
        second_ids = {o.source.material_id for o in second.observations}
        assert second_ids <= {"n1", "n2", "n3"}
        assert second_ids.isdisjoint({"m1", "m2", "m3"})
        assert len(second.observations) == len(first.observations)
        assert all(o.source.page_index == 1 for o in second.observations)

    def test_multipage_pdf_pages_do_not_collide_in_cache(self, tmp_path, gw_env):
        (tmp_path / "assets").mkdir(exist_ok=True)
        (tmp_path / "assets" / "cert.pdf").write_bytes(make_pdf_bytes(pages=2))
        store = TaskStore(tmp_path / "pdf_cache.sqlite3")
        gateway, calls = make_mock_gateway(tmp_path, ObservationBatch(), store=store)
        t1 = make_task(
            audit_unit_id="u-pdf-1",
            materials=[mref("p1", "certificate_pages", "assets/cert.pdf", "application/pdf", page=1)],
        )
        t2 = make_task(
            audit_unit_id="u-pdf-2",
            materials=[mref("p2", "certificate_pages", "assets/cert.pdf", "application/pdf", page=2)],
        )
        gateway.bind_task(t1, plan_identity="B001:3.0")
        gateway.observe(gap_all(["p1"]))
        gateway.bind_task(t2, plan_identity="B001:3.0")
        gateway.observe(gap_all(["p2"]))
        # Same PDF file bytes, different source page_index -> MUST be a cache miss!
        assert calls["count"] == 2
        assert gateway.usage_summary()["cache_hits"] == 0

    def test_different_content_is_a_cache_miss(self, tmp_path, gw_env):
        write_materials(tmp_path)
        store = TaskStore(tmp_path / "state.sqlite3")
        gateway, calls = make_mock_gateway(tmp_path, pass_batch(), store=store)
        gateway.bind_task(make_task())
        gateway.observe(gap_all(["m1", "m2", "m3"]))
        # mutate m3's content -> different sha -> cache miss
        (tmp_path / "assets" / "m3.png").write_bytes(make_png_bytes(color=(9, 9, 9)))
        gateway.observe(gap_all(["m1", "m2", "m3"]))
        assert calls["count"] == 2

    def test_cache_key_distinguishes_scope_and_model(self):
        base = dict(content_sha256s=["a"], model="m", prompt_hash="p",
                    schema_version="obs-v1", scope="label:model")
        other = dict(base, scope="certificate:material_number")
        assert cache_key(**base) != cache_key(**other)
        other = dict(base, model="other-model")
        assert cache_key(**base) != cache_key(**other)
        assert cache_key(**base) == cache_key(**dict(base, plan_identity="x" * 0 + ""))

    def test_annotate_target(self, tmp_path, gw_env):
        write_materials(tmp_path)
        annotation = json.dumps({"text": "QLAB－123456", "status": "value",
                                 "bbox": [0.2, 0.2, 0.6, 0.6]}, ensure_ascii=False)
        gateway, calls = make_mock_gateway(tmp_path, fixed_output=annotation)
        gateway.bind_task(make_task())
        result = gateway.annotate_target("材料编号", ["m3"])
        assert calls["count"] == 1
        assert result.text == "QLAB-123456"
        assert result.status == "value"
        assert result.source.material_id == "m3"
        assert result.bbox == (0.2, 0.2, 0.6, 0.6)

    def test_annotate_target_crop_remaps(self, tmp_path, gw_env):
        write_materials(tmp_path)
        annotation = json.dumps({"text": "X", "status": "value",
                                 "bbox": [0.0, 0.0, 1.0, 1.0]})
        gateway, _ = make_mock_gateway(tmp_path, fixed_output=annotation)
        gateway.bind_task(make_task())
        result = gateway.annotate_target("型号", ["m1"], crops={"m1": (0.5, 0.5, 1.0, 1.0)})
        assert result.bbox == pytest.approx((0.5, 0.5, 1.0, 1.0))


# ---------------------------------------------------------------------------
# AuditController end-to-end over the real gateway (MockTransport model)
# ---------------------------------------------------------------------------


def run_controller(tmp_path, batch, *, store: TaskStore | None = None,
                   mutate=None, statuses=None) -> tuple[dict, ZCodeObservationGateway]:
    write_materials(tmp_path)
    gateway, _ = make_mock_gateway(tmp_path, batch, store=store, statuses=statuses)
    task = make_task()
    gateway.bind_task(task, plan_identity="B001:3.0")
    controller = AuditController(plan_3_0(), gateway, store=store, run_id="e2e-run")
    return controller.run(task), gateway


def voided_label_batch() -> ObservationBatch:
    b = pass_batch()
    observations = []
    for o in b.observations:
        if o.entity_id == "L1" and o.predicate == "void_marked":
            o = o.model_copy(update={"raw_value": True, "normalized_value": True})
        observations.append(o)
    return ObservationBatch(entities=b.entities, observations=observations)


def batch_without_attachment() -> ObservationBatch:
    b = pass_batch()
    observations = [o for o in b.observations
                    if not (o.entity_id == "L1" and o.predicate == "attached_to_package")]
    return ObservationBatch(entities=b.entities, observations=observations)


class TestControllerEndToEnd:
    def test_pass_label_is_protocol_legal(self, tmp_path, gw_env):
        store = TaskStore(tmp_path / "s1.sqlite3")
        row, gateway = run_controller(tmp_path, pass_batch(), store=store)
        assert row["execution_status"] == "completed"
        assert row["label"] == "pass"
        assert row["applicability"] == "applicable"
        assert prediction_semantic_errors(row, make_task()) == []

    def test_reject_label_is_protocol_legal(self, tmp_path, gw_env):
        store = TaskStore(tmp_path / "s2.sqlite3")
        row, _ = run_controller(tmp_path, voided_label_batch(), store=store)
        assert row["execution_status"] == "completed"
        assert row["label"] == "reject"  # voided-only label is a proven violation
        assert prediction_semantic_errors(row, make_task()) == []

    def test_ambiguous_label_is_protocol_legal(self, tmp_path, gw_env):
        store = TaskStore(tmp_path / "s3.sqlite3")
        row, _ = run_controller(tmp_path, batch_without_attachment(), store=store)
        assert row["execution_status"] == "completed"
        assert row["label"] == "ambiguous"  # attachment unproven -> not reject/pass
        assert prediction_semantic_errors(row, make_task()) == []

    def test_gateway_failure_is_error_row_with_null_label(self, tmp_path, gw_env):
        store = TaskStore(tmp_path / "s4.sqlite3")
        row, _ = run_controller(tmp_path, pass_batch(), store=store,
                                statuses=[500])  # persistent upstream failure
        assert row["execution_status"] == "error"
        assert row["label"] is None
        assert row["applicability"] is None
        assert row["error_code"] == "gateway_http_error"
        assert prediction_semantic_errors(row, make_task()) == []

    def test_gateway_timeout_is_error_not_ambiguous(self, tmp_path, gw_env):
        store = TaskStore(tmp_path / "s5.sqlite3")
        row, _ = run_controller(tmp_path, pass_batch(), store=store,
                                statuses=["timeout", "timeout"])
        assert row["execution_status"] == "error"
        assert row["label"] is None
        assert row["error_code"] == "gateway_timeout"
