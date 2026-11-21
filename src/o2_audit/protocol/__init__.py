"""Protocol boundary: strict JSON IO, public semantic checks, safe material paths."""
from o2_audit.protocol.checks import (
    annotation_semantic_errors,
    input_semantic_errors,
    prediction_semantic_errors,
    visual_evidence_required,
)
from o2_audit.protocol.jsonio import (
    MAX_FILE_BYTES,
    MAX_LINE_BYTES,
    MAX_RECORDS,
    ProtocolError,
    read_json_strict,
    read_jsonl_strict,
)
from o2_audit.protocol.paths import MaterialPathError, resolve_material_path

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_LINE_BYTES",
    "MAX_RECORDS",
    "MaterialPathError",
    "ProtocolError",
    "annotation_semantic_errors",
    "input_semantic_errors",
    "prediction_semantic_errors",
    "read_json_strict",
    "read_jsonl_strict",
    "resolve_material_path",
    "visual_evidence_required",
]
