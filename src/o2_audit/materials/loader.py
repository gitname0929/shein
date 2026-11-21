"""Materials loading layer (Stage 03a).

The loader is the only component that turns protocol ``MaterialRef`` entries
into decoded pixels for the observation gateway. Responsibilities:

- Safe path resolution strictly under ``data_root`` via
  :func:`o2_audit.protocol.paths.resolve_material_path` (symlinks, traversal,
  absolute paths and missing files are rejected there).
- Bounded decoding: max file bytes, max decoded pixels (decompression-bomb
  guard) and declared-vs-actual format checks (PNG/JPEG/WEBP via Pillow,
  multi-page PDF via ``pypdfium2``).
- ``page_index`` is the 1-based page INSIDE the source file. For PDFs the
  exact page is rendered and the total PDF page count is recorded. This is a
  material locator — never the page number printed inside a document.
- Raw file ``content_sha256`` for deterministic extraction caching.
- Overly large images are downscaled for model transmission while the exact
  full-source width/height is preserved: normalized ``[0, 1]`` coordinates are
  scale-invariant, so they always map back to the original source image.
- Crop helpers: :func:`crop_region` and the exact inverse mapping
  :func:`map_crop_bbox_to_source` (a normalized bbox inside a crop maps back
  into full-source normalized coordinates).

All failures raise :class:`~o2_audit.domain.models.ExecutionError` so the
control layer records ``execution_status="error"`` instead of a verdict.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from o2_audit.domain.models import ExecutionError, MaterialRef, validate_bbox_tuple
from o2_audit.protocol.paths import MaterialPathError, resolve_material_path

try:  # pypdfium2 is a declared dependency; import lazily-friendly anyway.
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - environment misconfiguration
    pdfium = None  # type: ignore[assignment]

# Bounded decoding limits (AGENTS.md: prevent decompression bombs / oversized
# inputs). Raw files larger than MAX_FILE_BYTES are rejected before decoding.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
#: Longest transmitted image edge; larger sources are downscaled (never
#: upscaled) with normalized coordinates unchanged.
DEFAULT_MAX_EDGE = 2000

#: Bbox floats within this epsilon of the valid range are clamped (roundtrip
#: float noise), anything further out is rejected.
BBOX_EPSILON = 1e-6

_IMAGE_FORMATS: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}

_ENCODE_MIME: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
}


def _invalid_bbox_error(detail: str) -> ExecutionError:
    return ExecutionError("invalid_bbox", detail)


@dataclass
class LoadedMaterial:
    """One decoded, transmission-ready material slot.

    ``source_width``/``source_height`` are the EXACT full-source dimensions;
    ``image`` may be a downscaled (and/or cropped) view used for model
    transmission. Normalized ``[0, 1]`` coordinates are relative, so they map
    identically on both.
    """

    material_id: str
    role: str
    page_index: int  # 1-based page inside the SOURCE file (locator, not printed number)
    media_type: str  # declared protocol media type
    content_sha256: str
    file_size_bytes: int
    source_width: int
    source_height: int
    image: Image.Image
    pdf_page_count: int | None = None  # total pages of the source PDF, None for images
    crop_bbox: tuple[float, float, float, float] | None = None

    @property
    def is_crop(self) -> bool:
        return self.crop_bbox is not None


def _validate_page_index(page_index: Any) -> int:
    """1-based source page: strict JSON integer, bool/float rejected."""
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise ExecutionError(
            "invalid_page_index",
            f"page_index must be a JSON integer (bool/float rejected), got {page_index!r}",
        )
    if page_index < 1:
        raise ExecutionError(
            "invalid_page_index",
            f"page_index is 1-based and must be >= 1, got {page_index}",
        )
    return page_index


def _coerce_bbox(value: Any, *, clamp: bool) -> tuple[float, float, float, float]:
    """Validated normalized bbox; with ``clamp`` tiny epsilon violations are
    pulled into ``[0, 1]`` while everything grossly invalid (non-finite, bool,
    inverted, far out of range) raises ``invalid_bbox``."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise _invalid_bbox_error(f"bbox must be 4 numbers, got {value!r}")
    vals: list[float] = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise _invalid_bbox_error(f"bbox coordinates must be finite numbers (bool rejected): {value!r}")
        v = float(x)
        if clamp:
            if v < -BBOX_EPSILON or v > 1.0 + BBOX_EPSILON:
                raise _invalid_bbox_error(f"bbox coordinate out of [0, 1] range: {value!r}")
            vals.append(min(1.0, max(0.0, v)))
        else:
            if v < 0.0 or v > 1.0:
                raise _invalid_bbox_error(f"bbox coordinate out of [0, 1] range: {value!r}")
            vals.append(v)
    x1, y1, x2, y2 = vals
    if not (x1 < x2 and y1 < y2):
        raise _invalid_bbox_error(f"bbox must be ordered (x1<x2, y1<y2): {value!r}")
    return (x1, y1, x2, y2)


def map_crop_bbox_to_source(
    crop_bbox: Any, sub_bbox: Any
) -> tuple[float, float, float, float]:
    """Exact inverse crop mapping.

    ``crop_bbox`` is a normalized bbox in full-source coordinates, ``sub_bbox``
    a normalized bbox inside the cropped image. Returns the same region as a
    normalized bbox in FULL-SOURCE coordinates:

        source = crop_origin + sub * crop_size

    Tiny floating-point noise around 0/1 is clamped; inverted, non-finite,
    bool or far out-of-range boxes are rejected (coordinates are never
    fabricated).
    """
    cx1, cy1, cx2, cy2 = _coerce_bbox(crop_bbox, clamp=False)
    sx1, sy1, sx2, sy2 = _coerce_bbox(sub_bbox, clamp=True)
    return (
        cx1 + sx1 * (cx2 - cx1),
        cy1 + sy1 * (cy2 - cy1),
        cx1 + sx2 * (cx2 - cx1),
        cy1 + sy2 * (cy2 - cy1),
    )


def crop_region(
    image: Image.Image, crop_bbox: Any
) -> Image.Image:
    """Crop ``image`` to a normalized ``[x1, y1, x2, y2]`` bbox (pixel-clamped)."""
    x1, y1, x2, y2 = _coerce_bbox(crop_bbox, clamp=False)
    w, h = image.size
    left = max(0, min(w - 1, int(round(x1 * w))))
    top = max(0, min(h - 1, int(round(y1 * h))))
    right = max(left + 1, min(w, int(round(x2 * w))))
    bottom = max(top + 1, min(h, int(round(y2 * h))))
    return image.crop((left, top, right, bottom))


def encode_image_base64(image: Image.Image, fmt: str = "PNG") -> tuple[str, str]:
    """Encode a PIL image as base64 payload + MIME type for ``/v1/messages``
    image content blocks (PNG default; JPEG converts non-RGB modes)."""
    fmt_up = fmt.upper()
    if fmt_up not in _ENCODE_MIME:
        raise ExecutionError("material_encode_error", f"unsupported encode format: {fmt!r}")
    img = image
    if fmt_up == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, fmt_up)
    return base64.b64encode(buf.getvalue()).decode("ascii"), _ENCODE_MIME[fmt_up]


def _decode_image_bytes(
    data: bytes, declared_media_type: str, max_pixels: int
) -> Image.Image:
    """Decode PNG/JPEG/WEBP bytes with format + pixel-count verification."""
    expected_format = _IMAGE_FORMATS.get(declared_media_type)
    if expected_format is None:
        raise ExecutionError(
            "material_format_mismatch",
            f"unsupported image media_type: {declared_media_type!r}",
        )
    try:
        img = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as e:
        raise ExecutionError(
            "material_too_many_pixels",
            f"image exceeds decompression-bomb pixel guard: {e}",
        ) from e
    except Exception as e:  # PIL raises a zoo of exceptions on bad bytes
        raise ExecutionError("material_decode_error", f"image cannot be decoded: {e}") from e
    if img.format != expected_format:
        raise ExecutionError(
            "material_format_mismatch",
            f"declared {declared_media_type} but decoded format is {img.format!r}",
        )
    width, height = img.size
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ExecutionError(
            "material_too_many_pixels",
            f"decoded image {width}x{height} exceeds the {max_pixels}-pixel guard "
            "(decompression-bomb protection)",
        )
    try:
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception as e:
        raise ExecutionError("material_decode_error", f"image data cannot be read: {e}") from e
    return img


def _downscale_for_transmission(img: Image.Image, max_edge: int) -> Image.Image:
    """Downscale (never upscale) so the longest edge fits ``max_edge``.

    Only the transmitted view changes; normalized coordinates are relative and
    keep mapping to the exact full-source dimensions recorded on the
    :class:`LoadedMaterial`.
    """
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return img.resize(new_size, Image.LANCZOS)


def _render_pdf_page(
    data: bytes, page_index: int, max_edge: int
) -> tuple[Image.Image, int]:
    """Render the EXACT 1-based ``page_index`` of a PDF; also returns the
    total PDF page count. ``page_index`` is a material locator for the source
    file — it is never a printed document page number."""
    if pdfium is None:  # pragma: no cover - dependency guard
        raise ExecutionError("material_decode_error", "pypdfium2 is not available")
    try:
        doc = pdfium.PdfDocument(io.BytesIO(data))
    except Exception as e:
        raise ExecutionError("material_decode_error", f"PDF cannot be opened: {e}") from e
    try:
        page_count = len(doc)
        if page_index > page_count:
            raise ExecutionError(
                "invalid_page_index",
                f"page_index {page_index} out of range: PDF has {page_count} page(s)",
            )
        page = doc[page_index - 1]
        page_w, page_h = page.get_size()
        # Rendered longest edge never exceeds max_edge (or 4x for tiny pages).
        scale = min(4.0, max(0.05, max_edge / max(page_w, page_h, 1.0)))
        bitmap = page.render(scale=scale)
        img = bitmap.to_pil().convert("RGB")
    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError("material_decode_error", f"PDF page {page_index} cannot be rendered: {e}") from e
    finally:
        close = getattr(doc, "close", None)
        if callable(close):
            close()
    return img, page_count


def load_material(
    data_root: str | Path,
    material: MaterialRef,
    *,
    max_bytes: int = MAX_FILE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
    max_edge: int = DEFAULT_MAX_EDGE,
    crop_bbox: Any | None = None,
) -> LoadedMaterial:
    """Resolve, verify and decode one material entry of the bound task.

    Raises :class:`ExecutionError` (never a fabricated substitute) on unsafe
    paths, missing/oversized files, format mismatch, decompression-bomb
    dimensions, invalid page index or bad crop boxes.
    """
    crop: tuple[float, float, float, float] | None = None
    if crop_bbox is not None:
        crop = _coerce_bbox(crop_bbox, clamp=False)

    page_index = _validate_page_index(material.page_index)
    try:
        path: Path = resolve_material_path(Path(data_root), material.relative_path)
    except MaterialPathError as e:
        code = "material_not_found" if "missing" in str(e) else "material_path_invalid"
        raise ExecutionError(code, str(e)) from e

    try:
        file_size = path.stat().st_size
    except OSError as e:
        raise ExecutionError("material_not_found", f"material file not statable: {e}") from e
    if file_size > max_bytes:
        raise ExecutionError(
            "material_too_large",
            f"material file is {file_size} bytes, over the {max_bytes}-byte limit",
        )
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ExecutionError("material_not_found", f"material file unreadable: {e}") from e
    content_sha256 = hashlib.sha256(data).hexdigest()

    if material.media_type == "application/pdf":
        img, pdf_page_count = _render_pdf_page(data, page_index, max_edge)
        source_w, source_h = img.size
        pdf_page_count_final: int | None = pdf_page_count
    else:
        img = _decode_image_bytes(data, material.media_type, max_pixels)
        source_w, source_h = img.size
        pdf_page_count_final = None

    transmitted = _downscale_for_transmission(img, max_edge)
    if crop is not None:
        transmitted = crop_region(transmitted, crop)

    return LoadedMaterial(
        material_id=material.material_id,
        role=material.role,
        page_index=page_index,
        media_type=material.media_type,
        content_sha256=content_sha256,
        file_size_bytes=file_size,
        source_width=source_w,
        source_height=source_h,
        image=transmitted,
        pdf_page_count=pdf_page_count_final,
        crop_bbox=crop,
    )


__all__ = [
    "BBOX_EPSILON",
    "DEFAULT_MAX_EDGE",
    "LoadedMaterial",
    "MAX_FILE_BYTES",
    "MAX_IMAGE_PIXELS",
    "crop_region",
    "encode_image_base64",
    "load_material",
    "map_crop_bbox_to_source",
]
