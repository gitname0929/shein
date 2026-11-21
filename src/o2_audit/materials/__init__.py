"""Materials loading layer (Stage 03a): safe decoding of task materials.

See :mod:`o2_audit.materials.loader` for the security/limit contract. This
package never invents pixels or coordinates and never produces verdicts.
"""
from o2_audit.materials.loader import (
    BBOX_EPSILON,
    DEFAULT_MAX_EDGE,
    MAX_FILE_BYTES,
    MAX_IMAGE_PIXELS,
    LoadedMaterial,
    crop_region,
    encode_image_base64,
    load_material,
    map_crop_bbox_to_source,
)

__all__ = [
    "BBOX_EPSILON",
    "DEFAULT_MAX_EDGE",
    "MAX_FILE_BYTES",
    "MAX_IMAGE_PIXELS",
    "LoadedMaterial",
    "crop_region",
    "encode_image_base64",
    "load_material",
    "map_crop_bbox_to_source",
]
