# Observation vocabulary (`src/o2_audit/domain/vocabulary.py`)

The structured vocabulary is the single contract between Stage 03's visual
gateway (producer) and the 62 rule evaluators (consumers). The gateway reports
**observations only** — never labels, compliance flags or decisions. Every
predicate string an evaluator may rely on is declared once in the `P` namespace
and grouped in `GROUPS`; free-form predicate strings are a programming error.

## Predicate groups (`GROUPS`)

| Group | Predicates | Expected value type |
| --- | --- | --- |
| `text_field` | `verbatim_text`, `language_text`, `step_list`, `qr_present` | `str` / `list[str]` / `list[(int, str)]` / `bool` |
| `coverage` | `boundary_visible`, `interior_obstructed`, `visible_faces`, `printed_page_number`, `total_printed_pages`, `material_authenticity`, `supplement_field_identity`, `same_object_match` | `bool` / `bool` / `list[str]` / `int` / `int` / `"photo" \| "design" \| "render"` / `str` / `bool` |
| `marks` | `shape_type`, `is_filled`, `is_closed`, `color_mode`, `mark_color`, `mark_count`, `mark_bbox`, `label_bbox`, `package_bbox`, `line_crosses_field`, `view_type`, `relative_position`, `height_mm`, `width_mm`, `area_ratio` | `str` / `bool` / `bool` / `"color" \| "grayscale"` / `str` / `int` / `bbox` / `bbox` / `bbox` / `list[str]` / `str` / `str` / number / number / number |
| `numeric` | `measured_value`+`measured_unit`, `declared_value`+`declared_unit` | number + unit `str` (always paired) |
| `documents` | `void_marked`, `issue_date`, `withdrawal_date`, `valid_from`, `valid_until`, `covered_models`, `component_model_map`, `model_spec_map`, `contact_info`, `document_number`, `alternative_sets` | `bool` / date `str` / date `str` / date `str` / date `str` / `list[str]` / `dict` / `dict` / `str` / `str` / list of alternative fact sets |

Identity / placement / quantity predicates (`model`, `main_model`,
`compatible_models`, `old_model`, `replacement_map`, `serial_number`,
`serial_carrier`, `material_number`, `carrier`, `attached_to_package`,
`batch_number`, `batch_interval`, `piece_count`, `support_url`,
`product_name_cn`, `product_name_en`, `component_models`,
`component_percentages`, `capacity`, `net_weight`, `total_weight`,
`front_weight`, `back_weight`, `adjustable_height_min/max`, `height_ratio`,
`production_date`, `document_version`, `package_spec`, `box_color`) and the
cross-material extras (`front_fully_shown`, `figure_model`,
`figure_material_number`) are declared in `P` as well; they are the
rule-family vocabulary shared by F01–F10 evaluators.

## Entity kinds (`EntityKind`)

`label` (a physical tag/sticker with a recognizable border), `package`
(outer package / exercise box), `certificate` (proof document page),
`document` (instruction sheets, certificate pages treated as documents),
`object` (the product body, individual visual marks). Distinct physical
objects are distinct entities — see "Entity separation" below.

## Observation status semantics (`STATUS_CONTRACT`)

| Status | Meaning |
| --- | --- |
| `readable` | Value observed; `raw_value` (transcription) and `normalized_value` (business comparison value) are available. |
| `confirmed_missing` | Absent in a **complete, clear** check range. Legal **only** with `scope_complete=True`; the model validator rejects the combination otherwise. |
| `unreadable` | Present but not readable. **Never** equal to missing; never inferred from an empty OCR result. |
| `incomplete_scope` | The required check range was not fully visible; absence cannot be claimed. |
| `identity_unknown` | Observed claims conflict, or the entity identity cannot be pinned. |

An empty OCR result never proves absence; only `confirmed_missing` with
`scope_complete=True` does.

## Provenance

Every observation carries `source`: `material_id`, `page_index` and, where the
region could be located, a normalized `bbox`. Coordinates are **never
invented** — an observation whose region could not be located has
`source.bbox=None`. Evidence rows inherit provenance from observations.

## Raw vs normalized values

`raw_value` keeps the original transcription (original case, spaces,
characters). `normalized_value` carries the business comparison value produced
with the shared operators (NFKC + strip for annotations; model normalization
for models, which preserves O/0 and I/1 distinctions). The two stay separate
and are never mixed as if they were the same comparison space.

## Entity separation

- One photo may contain **several independent labels**; each recognizable
  border is its own `kind=label` entity with its own `entity_id`.
- Fields of two separate labels are never merged (R021 same-label, B001
  same-label checks rely on this).
- A `作废` (void) mark belongs to its own label entity only — it is never
  applied to the whole photo or to sibling labels (R029/R054 per-entity
  `void_marked`).

## Printed page number vs source `page_index`

`page_index` locates a page **inside the source file** (a material locator;
usually `1` for a single PNG). `printed_page_number` is the page number
**printed inside the certificate document**. R003 compares printed page
numbers against the declared total; several PNGs with `page_index=1` are NOT
duplicate certificate pages.
