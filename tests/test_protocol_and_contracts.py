"""Protocol 1.3.1 strictness, safe material paths, vendored validator parity."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from o2_audit.domain.models import (
    AuditInput,
    Evidence,
    MaterialRef,
    Metadata,
    Observation,
    ObservationSource,
)
from o2_audit.protocol.jsonio import ProtocolError, dump_json_strict, read_json_strict
from o2_audit.protocol.paths import MaterialPathError, resolve_material_path
from o2_audit.resources import protocol_dir, rules_dir, student_spec_path
from o2_public_contracts import contracts as public_contracts

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import make_task  # noqa: E402


# -- strict JSON / JSONL ------------------------------------------------------


def test_strict_loads_rejects_duplicate_keys():
    with pytest.raises(public_contracts.ContractError):
        public_contracts.strict_loads('{"a": 1, "a": 2}')


def test_strict_loads_rejects_nan_and_infinity():
    for bad in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
        with pytest.raises(public_contracts.ContractError):
            public_contracts.strict_loads(bad)


def test_read_json_strict_wrapper(tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_strict(good) == {"a": 1}
    dup = tmp_path / "dup.json"
    dup.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ProtocolError):
        read_json_strict(dup)
    nan = tmp_path / "nan.json"
    nan.write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(ProtocolError):
        read_json_strict(nan)


def test_dump_json_strict_rejects_nonfinite():
    with pytest.raises(ProtocolError):
        dump_json_strict({"x": float("nan")})


# -- strict typed scalars -----------------------------------------------------


def _material(**overrides):
    fields = dict(material_id="m1", role="package_photos", relative_path="a/b.png",
                  media_type="image/png", page_index=1)
    fields.update(overrides)
    return MaterialRef(**fields)


def test_page_index_rejects_bool_float_and_negative():
    with pytest.raises(Exception):
        _material(page_index=True)
    with pytest.raises(Exception):
        _material(page_index=1.0)
    with pytest.raises(Exception):
        _material(page_index=-1)
    with pytest.raises(Exception):
        Evidence(material_id="m1", page_index=True, fact="f")
    with pytest.raises(Exception):
        ObservationSource(material_id="m1", page_index=2.5)


def test_evidence_bbox_strict():
    ok = Evidence(material_id="m1", page_index=1, fact="f", bbox=[0.1, 0.2, 0.5, 0.9])
    assert ok.bbox == (0.1, 0.2, 0.5, 0.9)
    for bad in ([1, 0, 0.5, 0.9],  # unordered x
                [0.1, 0.9, 0.5, 0.2],  # unordered y
                [True, 0.2, 0.5, 0.9],  # bool coordinate
                [-0.1, 0.2, 0.5, 0.9],  # out of range
                [0.1, 0.2, 1.5, 0.9],
                [0.1, float("nan"), 0.5, 0.9],
                [0.1, 0.2, 0.5]):  # wrong arity
        with pytest.raises(Exception):
            Evidence(material_id="m1", page_index=1, fact="f", bbox=bad)


def test_observation_source_bbox_strict():
    assert ObservationSource(material_id="m1", page_index=1,
                             bbox=(0.0, 0.0, 1.0, 1.0)).bbox == (0.0, 0.0, 1.0, 1.0)
    with pytest.raises(Exception):
        ObservationSource(material_id="m1", page_index=1, bbox=[0.5, 0.5, 0.5, 0.5])


def test_confirmed_missing_requires_complete_scope():
    with pytest.raises(Exception):
        Observation(entity_id="L1", predicate="model",
                    status="confirmed_missing", scope_complete=False)
    ok = Observation(entity_id="L1", predicate="model",
                     status="confirmed_missing", scope_complete=True)
    assert ok.status.value == "confirmed_missing"


# -- metadata flat-dict deserialization ---------------------------------------


def test_metadata_flat_dict_populates_values():
    task = AuditInput.model_validate({
        "audit_unit_id": "u-1",
        "sample_id": "s-1",
        "rule_id": "B001",
        "rule_version": "3.0",
        "review_date": "2026-06-15",
        "metadata": {"market": "Q", "category": "桌面灯", "extra_unknown": 7},
        "materials": [],
    })
    assert task.metadata.values["market"] == "Q"
    assert task.metadata.values["category"] == "桌面灯"
    assert task.metadata.values["extra_unknown"] == 7
    # nothing leaked into model_extra
    assert task.metadata.model_extra in (None, {})
    assert Metadata(values={"market": "Q"}).values == {"market": "Q"}
    assert Metadata(**{"market": "Q"}).values == {"market": "Q"}


# -- public contract functions on raw JSON -------------------------------------


def test_public_input_errors_reject_bool_page():
    task = make_task()
    raw = task.model_dump(mode="json")
    raw["metadata"] = task.metadata.values
    assert public_contracts.input_errors(raw) == []
    raw["materials"][0]["page_index"] = True
    assert public_contracts.input_errors(raw) == ["material_type_or_page"]


def test_public_input_errors_reject_float_page():
    task = make_task()
    raw = task.model_dump(mode="json")
    raw["metadata"] = task.metadata.values
    raw["materials"][0]["page_index"] = 1.0
    assert public_contracts.input_errors(raw) == ["material_type_or_page"]


def test_public_prediction_errors_require_valid_label():
    task = make_task()
    raw = task.model_dump(mode="json")
    raw["metadata"] = task.metadata.values
    base = {
        "audit_unit_id": task.audit_unit_id,
        "rule_id": task.rule_id,
        "rule_version": task.rule_version,
        "execution_status": "completed",
        "label": "pass",
        "applicability": "not_applicable",
        "reason_code": "rule_not_applicable",
        "reason": "不在适用范围",
        "evidence": [],
        "artifact_version": "x",
        "trace_id": "t",
        "latency_ms": 1,
        "cost_units": None,
    }
    assert public_contracts.prediction_errors(base, raw) == []
    bad = dict(base, label="ok")
    assert "invalid_label" in public_contracts.prediction_errors(bad, raw)
    inconsistent = dict(base, reason_code="other")
    assert "inconsistent_not_applicable" in public_contracts.prediction_errors(inconsistent, raw)


# -- safe material paths -------------------------------------------------------


def test_safe_material_path_accepts_plain_relative(tmp_path):
    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "f.png"
    target.write_bytes(b"x")
    assert resolve_material_path(tmp_path, "sub/f.png") == target.resolve()


@pytest.mark.parametrize("bad", [
    "../escape.png",
    "a/../../escape.png",
    "/absolute/path.png",
    "back\\slash.png",
    "C:/drive/path.png",
    "C:\\drive\\path.png",
    "./dot.png",
    "a//b.png",
    "",
])
def test_safe_material_path_rejects_unsafe(tmp_path, bad):
    with pytest.raises(MaterialPathError):
        resolve_material_path(tmp_path, bad)


def test_safe_material_path_rejects_missing_file(tmp_path):
    with pytest.raises(MaterialPathError):
        resolve_material_path(tmp_path, "missing.png")


def test_safe_material_path_rejects_symlink(tmp_path):
    real = tmp_path / "real.png"
    real.write_bytes(b"x")
    link = tmp_path / "link.png"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(MaterialPathError):
        resolve_material_path(tmp_path, "link.png")


def test_safe_material_path_rejects_symlinked_directory(tmp_path):
    (tmp_path / "inside").mkdir()
    real = tmp_path / "outside"
    real.mkdir()
    link = tmp_path / "jump"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    (real / "f.png").write_bytes(b"x")
    with pytest.raises(MaterialPathError):
        resolve_material_path(tmp_path, "jump/f.png")


# -- vendored validator verified against the organizer hash fixture ------------


def test_vendored_artifact_hash_matches_public_fixture():
    from o2_public_contracts import artifact_hash

    fixture_dir = protocol_dir() / "hash_fixture"
    expected = json.loads(
        (protocol_dir() / "hash_fixture_expected.json").read_text(encoding="utf-8"))
    inv = artifact_hash.inventory(fixture_dir)
    assert inv == expected["inventory"]
    assert artifact_hash.canonical(inv).decode("utf-8") == expected["canonical_utf8_text"]
    assert artifact_hash.tree_digest(fixture_dir) == expected["tree_sha256"]


# -- repo-local resources ------------------------------------------------------


def test_repo_local_resources_exist():
    assert (rules_dir() / "基础规则B001.md").is_file()
    assert (rules_dir() / "基础规则B001_3.0.json").is_file()
    assert (rules_dir() / "基础规则B001_3.1.json").is_file()
    assert (protocol_dir() / "input.schema.json").is_file()
    assert (protocol_dir() / "output.schema.json").is_file()
    assert student_spec_path().is_file()
