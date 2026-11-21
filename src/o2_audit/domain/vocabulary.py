"""Structured observation vocabulary shared by all 62 rules.

Stage 03's visual gateway must emit facts in THIS vocabulary so every rule
evaluator consumes them without rule-ID branching in the gateway. The gateway
reports observations only (never decisions); predicates below are the
canonical strings used on :class:`~o2_audit.domain.models.Observation`.

Conventions every gateway producer must follow:

1. **Provenance**: each observation carries ``source.material_id``,
   ``source.page_index`` and, where available, ``source.bbox``. Coordinates
   for regions that could not be located are never invented.
2. **Transcription vs normalization**: ``raw_value`` keeps the original
   transcription; ``normalized_value`` carries the business comparison value
   produced with shared operators. Both stay separate.
3. **Entity separation**: distinct physical labels / pages / units are
   distinct entities (``entity_id``), even inside one photo. Fields of two
   separate labels are never merged; a ``作废`` mark belongs to its own label
   entity only.
4. **Statuses**: :class:`~o2_audit.domain.models.ObservationStatus`
   distinguishes ``readable``, ``confirmed_missing`` (legal ONLY with
   ``scope_complete=True`` after the full clear check range was visible),
   ``unreadable``, ``incomplete_scope`` and ``identity_unknown``. An empty
   OCR result never proves absence.
"""

from __future__ import annotations


class P:
    """Canonical observation predicate strings (namespace of str constants)."""

    # -- text / field observations ------------------------------------------
    #: transcribed text of a fixed warning/instruction sentence
    VERBATIM_TEXT = "verbatim_text"
    #: language-declared sentences visible on the material (list of str)
    LANGUAGE_TEXT = "language_text"
    #: ordered assembly steps (list of (number, text) / "1 连接底座" entries)
    STEP_LIST = "step_list"
    #: the material carries only a QR code at the relevant location (bool)
    QR_PRESENT = "qr_present"

    # -- object / boundary / face coverage & obstruction ---------------------
    #: all four boundaries of the declared main face are visible (bool)
    BOUNDARY_VISIBLE = "boundary_visible"
    #: interior content (text/fields) is obstructed (bool); NOT a boundary fact
    INTERIOR_OBSTRUCTED = "interior_obstructed"
    #: check-face identifiers clearly recognizable in submitted photos (list)
    VISIBLE_FACES = "visible_faces"
    #: page number PRINTED INSIDE the certificate document (int; NOT the
    #: source-file page_index, which stays a material locator)
    PRINTED_PAGE_NUMBER = "printed_page_number"
    #: total page count printed on the document (int)
    TOTAL_PRINTED_PAGES = "total_printed_pages"
    #: evidence source class of a material: photo | design | render
    MATERIAL_AUTHENTICITY = "material_authenticity"
    #: business anchor (model / material number) shown by a supplement view
    SUPPLEMENT_FIELD_IDENTITY = "supplement_field_identity"
    #: the two views show the same physical object/field (bool)
    SAME_OBJECT_MATCH = "same_object_match"

    # -- visual marks & geometry ---------------------------------------------
    #: mark shape class: circle | square | triangle | star | dot | ...
    SHAPE_TYPE = "shape_type"
    #: mark is filled/solid (bool)
    IS_FILLED = "is_filled"
    #: outline is closed (bool)
    IS_CLOSED = "is_closed"
    #: photo color mode: color | grayscale
    COLOR_MODE = "color_mode"
    #: perceived mark color: blue | yellow | ...
    MARK_COLOR = "mark_color"
    #: number of instances of the mark in the declared scope (int)
    MARK_COUNT = "mark_count"
    #: normalized bbox of the mark
    MARK_BBOX = "mark_bbox"
    #: normalized bbox of a label/field
    LABEL_BBOX = "label_bbox"
    #: normalized bbox of the package face in the view
    PACKAGE_BBOX = "package_bbox"
    #: which fields the opening tear line crosses (list of field names;
    #: entries may be field names or the literal blank_margin)
    LINE_CROSSES_FIELD = "line_crosses_field"
    #: declared view type: front | back | side | top ...
    VIEW_TYPE = "view_type"
    #: relative position of a mark/label: upper_half | lower_half | on_midline
    #: | left_of | right_of | above | below | adjacent | inside | outside
    RELATIVE_POSITION = "relative_position"
    HEIGHT_MM = "height_mm"
    WIDTH_MM = "width_mm"
    #: measured ratio (mark height / reference char height; area share, ...)
    AREA_RATIO = "area_ratio"

    # -- warning / instruction content ---------------------------------------
    INDOOR_ONLY_TEXT = "indoor_only_text"
    OUTDOOR_DURABLE_TEXT = "outdoor_durable_text"
    DISASSEMBLY_CLAUSES = "disassembly_clauses"
    CONTENTS_LIST = "contents_list"
    TEXT_INSIDE_MARK = "text_inside_mark"
    INSIDE_TRIANGLE = "inside_triangle"

    # -- numeric / unit pairs --------------------------------------------------
    #: measured value (number) - always paired with MEASURED_UNIT
    MEASURED_VALUE = "measured_value"
    MEASURED_UNIT = "measured_unit"
    DECLARED_VALUE = "declared_value"
    DECLARED_UNIT = "declared_unit"

    # -- documents / tables / certificates -------------------------------------
    VOID_MARKED = "void_marked"
    ISSUE_DATE = "issue_date"
    WITHDRAWAL_DATE = "withdrawal_date"
    VALID_FROM = "valid_from"
    VALID_UNTIL = "valid_until"
    COVERED_MODELS = "covered_models"
    COMPONENT_MODEL_MAP = "component_model_map"
    MODEL_SPEC_MAP = "model_spec_map"
    CONTACT_INFO = "contact_info"
    DOCUMENT_NUMBER = "document_number"
    ALTERNATIVE_SETS = "alternative_sets"

    # -- identity / placement / quantities (rule-family vocabulary) -----------
    MODEL = "model"
    MAIN_MODEL = "main_model"
    COMPATIBLE_MODELS = "compatible_models"
    OLD_MODEL = "old_model"
    REPLACEMENT_MAP = "replacement_map"
    SERIAL_NUMBER = "serial_number"
    SERIAL_CARRIER = "serial_carrier"
    MATERIAL_NUMBER = "material_number"
    CARRIER = "carrier"
    ATTACHED_TO_PACKAGE = "attached_to_package"
    BATCH_NUMBER = "batch_number"
    BATCH_INTERVAL = "batch_interval"
    PIECE_COUNT = "piece_count"
    SUPPORT_URL = "support_url"
    PRODUCT_NAME_CN = "product_name_cn"
    PRODUCT_NAME_EN = "product_name_en"
    COMPONENT_MODELS = "component_models"
    COMPONENT_PERCENTAGES = "component_percentages"
    CAPACITY = "capacity"
    NET_WEIGHT = "net_weight"
    TOTAL_WEIGHT = "total_weight"
    FRONT_WEIGHT = "front_weight"
    BACK_WEIGHT = "back_weight"
    HEIGHT_MIN = "adjustable_height_min"
    HEIGHT_MAX = "adjustable_height_max"
    HEIGHT_RATIO = "height_ratio"
    PRODUCTION_DATE = "production_date"
    DOCUMENT_VERSION = "document_version"
    PACKAGE_SPEC = "package_spec"
    BOX_COLOR = "box_color"

    # -- placement / cross-material extras ------------------------------------
    #: the front face of the package is fully shown in submitted photos (bool)
    FRONT_FULLY_SHOWN = "front_fully_shown"
    #: main model text visible on the certificate appendix figure
    FIGURE_MODEL = "figure_model"
    #: material number visible on the certificate appendix figure
    FIGURE_MATERIAL_NUMBER = "figure_material_number"


# Predicates every evaluator may rely on, grouped by the vocabulary contract
# section of docs/observation_vocabulary.md.
GROUPS: dict[str, tuple[str, ...]] = {
    "text_field": (P.VERBATIM_TEXT, P.LANGUAGE_TEXT, P.STEP_LIST, P.QR_PRESENT),
    "coverage": (P.BOUNDARY_VISIBLE, P.INTERIOR_OBSTRUCTED, P.VISIBLE_FACES,
                 P.PRINTED_PAGE_NUMBER, P.TOTAL_PRINTED_PAGES,
                 P.MATERIAL_AUTHENTICITY, P.SUPPLEMENT_FIELD_IDENTITY,
                 P.SAME_OBJECT_MATCH),
    "marks": (P.SHAPE_TYPE, P.IS_FILLED, P.IS_CLOSED, P.COLOR_MODE, P.MARK_COLOR,
              P.MARK_COUNT, P.MARK_BBOX, P.LABEL_BBOX, P.PACKAGE_BBOX,
              P.LINE_CROSSES_FIELD, P.VIEW_TYPE, P.RELATIVE_POSITION,
              P.HEIGHT_MM, P.WIDTH_MM, P.AREA_RATIO),
    "numeric": (P.MEASURED_VALUE, P.MEASURED_UNIT, P.DECLARED_VALUE, P.DECLARED_UNIT),
    "documents": (P.VOID_MARKED, P.ISSUE_DATE, P.WITHDRAWAL_DATE, P.VALID_FROM,
                  P.VALID_UNTIL, P.COVERED_MODELS, P.COMPONENT_MODEL_MAP,
                  P.MODEL_SPEC_MAP, P.CONTACT_INFO, P.DOCUMENT_NUMBER,
                  P.ALTERNATIVE_SETS),
}

STATUS_CONTRACT: dict[str, str] = {
    "readable": "value observed; raw and normalized values available",
    "confirmed_missing": "absent in a COMPLETE CLEAR check range (scope_complete=True required)",
    "unreadable": "present but not readable; never equal to missing",
    "incomplete_scope": "required check range was not fully visible",
    "identity_unknown": "observed claims conflict or entity identity cannot be pinned",
}

__all__ = ["P", "GROUPS", "STATUS_CONTRACT"]
