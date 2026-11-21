"""ZCode multimodal observation gateway (Stage 03a).

``ZCodeObservationGateway`` implements the Stage 01
:class:`~o2_audit.runtime.gateway.ObservationGateway` boundary with the real
execution stack: materials loader → ZCode ``/v1/messages`` multimodal client
→ strict parser → typed :class:`ObservationBatch`. It reports observations
ONLY — never labels, compliance flags or decisions.

Key invariants:

- Materials are loaded through :mod:`o2_audit.materials.loader` (safe paths,
  bounded decoding, exact 1-based source ``page_index``).
- Optional crop regions are remapped back into full-source normalized
  coordinates; coordinates are never invented.
- Extraction results may be cached content-addressed in the workspace SQLite
  (never inside frozen artifacts). **Cache rebinding invariant**: on a cache
  hit, ``source.material_id`` / ``source.page_index`` are ALWAYS rebound to
  the current task's legal ``MaterialRef`` (matched by content SHA256), so a
  cached observation can never leak another audit unit's material IDs.
- Usage statistics (calls / tokens / latency / retries / cache hits) are
  tracked without logging secrets.
- ``annotate_target`` exposes Stage 03b target-directed extraction over the
  same client/config/usage accounting.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    Entity,
    ExecutionError,
    MaterialRef,
    NeedEvidence,
    Observation,
    ObservationSource,
)
from o2_audit.materials.loader import (
    DEFAULT_MAX_EDGE,
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    LoadedMaterial,
    encode_image_base64,
    load_material,
    map_crop_bbox_to_source,
)
from o2_audit.model_gateway.cache import (
    SCHEMA_VERSION,
    ExtractionCache,
    cache_key,
)
from o2_audit.model_gateway.client import MessagesClient, UsageStats
from o2_audit.model_gateway.parser import (
    AnnotationResult,
    BboxRemapper,
    parse_annotation,
    parse_observation_batch,
)
from o2_audit.model_gateway.prompts import (
    annotation_system_prompt,
    build_annotation_prompt,
    build_observation_prompt,
    observation_system_prompt,
    prompt_fingerprint,
)
from o2_audit.runtime.gateway import ObservationBatch


class ZCodeObservationGateway:
    """Real multimodal gateway over the ZCode ``/v1/messages`` API.

    Bind one audit unit with :meth:`bind_task` (supplying the ``data_root``
    the task's material paths resolve against), then serve ``observe`` /
    ``annotate_target`` calls. Failures raise :class:`ExecutionError` so the
    control layer records ``execution_status="error"`` with ``label=None``.
    """

    def __init__(
        self,
        client: MessagesClient,
        *,
        data_root: str | Path,
        cache: ExtractionCache | None = None,
        max_edge: int = DEFAULT_MAX_EDGE,
        max_bytes: int = MAX_FILE_BYTES,
        max_pixels: int = MAX_IMAGE_PIXELS,
    ) -> None:
        self._client = client
        self._data_root = Path(data_root)
        self._cache = cache
        self._max_edge = max_edge
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels
        self._materials: dict[str, MaterialRef] = {}
        self._plan_identity = ""
        self.last_parse_report: Any = None
        self.last_cache_dropped = 0
        self.usage = UsageStats()
        self.cache_hits = 0

    # -- binding ---------------------------------------------------------------

    def bind_task(self, task: AuditInput, *, plan_identity: str = "") -> None:
        """Bind the audit unit whose ``MaterialRef`` entries observations may
        cite. ``plan_identity`` (e.g. ``"B001:3.0"``) keeps rule-dependent
        extraction in a separate cache namespace."""
        self._materials = {m.material_id: m for m in task.materials}
        self._plan_identity = plan_identity

    # -- helpers -----------------------------------------------------------------

    def _candidate_materials(self, request: NeedEvidence) -> list[MaterialRef]:
        candidates = [
            self._materials[mid]
            for mid in request.candidate_material_ids
            if mid in self._materials
        ]
        return candidates or list(self._materials.values())

    def _load(self, materials: list[MaterialRef],
              crops: dict[str, Any] | None) -> list[LoadedMaterial]:
        loaded: list[LoadedMaterial] = []
        for m in materials:
            loaded.append(load_material(
                self._data_root, m,
                max_bytes=self._max_bytes,
                max_pixels=self._max_pixels,
                max_edge=self._max_edge,
                crop_bbox=(crops or {}).get(m.material_id),
            ))
        return loaded

    def _remapper(
        self, crops: dict[str, Any] | None
    ) -> BboxRemapper | None:
        if not crops:
            return None

        def remap(material_id: str, bbox: tuple[float, float, float, float]
                  ) -> tuple[float, float, float, float] | None:
            crop = crops.get(material_id)
            if crop is None:
                return bbox  # uncropped material: coordinates are already source-wide
            try:
                return map_crop_bbox_to_source(crop, bbox)
            except ExecutionError:
                return None

        return remap

    def _usage_from(self) -> None:
        # The client accumulates its own usage; the gateway mirror exposes one
        # merged summary (plus cache accounting).
        self.usage.calls = self._client.usage.calls
        self.usage.retries = self._client.usage.retries
        self.usage.input_tokens = self._client.usage.input_tokens
        self.usage.output_tokens = self._client.usage.output_tokens
        self.usage.latency_ms = self._client.usage.latency_ms

    def usage_summary(self) -> dict[str, int]:
        """Merged usage accounting (calls/retries/tokens/latency/cache hits)."""
        summary = self._client.usage.summary()
        summary["cache_hits"] = self.cache_hits
        return summary

    # -- observation round ---------------------------------------------------------

    def observe(
        self,
        request: NeedEvidence,
        crops: dict[str, tuple[float, float, float, float]] | None = None,
    ) -> ObservationBatch:
        """Inspect the requested materials and return observations ONLY.

        ``crops`` optionally maps ``material_id -> normalized crop bbox``: the
        model then sees the cropped view and its bboxes are remapped back into
        full-source coordinates before any observation is recorded.
        """
        if not self._materials:
            raise ExecutionError(
                "gateway_not_bound",
                "no audit unit bound: call bind_task(task) before observe()")
        materials = self._candidate_materials(request)
        loaded = self._load(materials, crops)
        allowed = {m.material_id: m for m in materials}

        system = observation_system_prompt()
        user_prompt = build_observation_prompt(request, materials)
        prompt_hash = prompt_fingerprint(request, materials, system)
        # Crops and content hashes in the cache key include page_index so
        # multi-page PDFs with identical file bytes never collide across pages.
        crops_for_key = (
            {f"{lm.content_sha256}#p{lm.page_index}": [round(float(v), 6) for v in crops[lm.material_id]]
             for lm in loaded if lm.material_id in crops}
            if crops else None
        )
        key = cache_key(
            content_sha256s=[f"{lm.content_sha256}#p{lm.page_index}" for lm in loaded],
            model=self._client.config.model,
            prompt_hash=prompt_hash,
            schema_version=SCHEMA_VERSION,
            scope=f"{request.entity_kind.value}:{request.predicate}",
            plan_identity=self._plan_identity,
            crops=crops_for_key,
        )

        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                self.cache_hits += 1
                return self._rebind_cached(hit, loaded, allowed)

        images = [encode_image_base64(lm.image, "PNG") for lm in loaded]
        reply = self._client.messages(system, user_prompt, images)
        self._usage_from()
        batch, report = parse_observation_batch(
            reply.text, allowed, bbox_remapper=self._remapper(crops))
        self.last_parse_report = report

        if self._cache is not None:
            self._cache.put(key, self._cache_payload(batch, loaded))
        return batch

    # -- cache payload / rebinding -------------------------------------------------

    @staticmethod
    def _cache_payload(batch: ObservationBatch, loaded: list[LoadedMaterial]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "entities": [e.model_dump(mode="json") for e in batch.entities],
            "observations": [o.model_dump(mode="json", exclude_none=False) for o in batch.observations],
            # slot map: content hash -> the (material_id, page_index) the
            # observation cited when cached; used to rebind on retrieval.
            "slots": [
                {"content_sha256": lm.content_sha256,
                 "material_id": lm.material_id,
                 "page_index": lm.page_index}
                for lm in loaded
            ],
        }

    def _rebind_cached(
        self,
        payload: dict[str, Any],
        loaded: list[LoadedMaterial],
        allowed: dict[str, MaterialRef],
    ) -> ObservationBatch:
        """Rebind cached observations to the CURRENT unit's legal materials.

        Matching is by ``(content_sha256, page_index)`` (via the stored slot
        map); a cached observation whose source content/page is not among the
        current materials is dropped rather than leaking a foreign material ID."""
        slot_to_ref = {(lm.content_sha256, lm.page_index): lm for lm in loaded}
        old_id_to_slot = {
            slot.get("material_id"): (slot.get("content_sha256"), slot.get("page_index"))
            for slot in payload.get("slots", [])
            if isinstance(slot, dict)
        }
        entities = [dict(e) for e in payload.get("entities", []) if isinstance(e, dict)]
        observations: list[dict[str, Any]] = []
        dropped = 0
        for raw in payload.get("observations", []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            source = item.get("source")
            if isinstance(source, dict) and source.get("material_id") is not None:
                slot = old_id_to_slot.get(source.get("material_id"))
                lm = slot_to_ref.get(slot) if slot is not None else None
                if lm is None:
                    dropped += 1  # foreign material content/page: never leak its ID
                    continue
                new_source = dict(source)
                new_source["material_id"] = lm.material_id
                new_source["page_index"] = lm.page_index
                item["source"] = new_source
            observations.append(item)
        # Rebind diagnostics (no fresh parse report exists for cache hits).
        self.last_cache_dropped = dropped
        return ObservationBatch(
            entities=[Entity.model_validate(e) for e in entities],
            observations=[Observation.model_validate(o) for o in observations],
        )

    # -- Stage 03b: target-directed annotation --------------------------------------

    def annotate_target(
        self,
        target: str,
        material_ids: list[str],
        *,
        target_hint: str = "",
        crops: dict[str, tuple[float, float, float, float]] | None = None,
    ) -> AnnotationResult:
        """Extract ONE target field verbatim (Stage 03b annotation).

        Returns an :class:`AnnotationResult` with ``status`` in
        ``value|unreadable|invalid_value`` and a bbox already remapped into
        full-source normalized coordinates (None when not locatable).
        """
        if not self._materials:
            raise ExecutionError(
                "gateway_not_bound",
                "no audit unit bound: call bind_task(task) before annotate_target()")
        materials = [
            self._materials[mid] for mid in material_ids if mid in self._materials
        ] or list(self._materials.values())
        loaded = self._load(materials, crops)
        allowed = {m.material_id: m for m in materials}
        system = annotation_system_prompt()
        user_prompt = build_annotation_prompt(target, materials, target_hint=target_hint)
        images = [encode_image_base64(lm.image, "PNG") for lm in loaded]
        reply = self._client.messages(system, user_prompt, images)
        self._usage_from()
        result, report = parse_annotation(
            reply.text, allowed, bbox_remapper=self._remapper(crops))
        self.last_parse_report = report
        return result


__all__ = [
    "AnnotationResult",
    "ObservationBatch",
    "ZCodeObservationGateway",
]
