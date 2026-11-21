"""Typed B001 rule plans (versions 3.0 / 3.1).

Only the version-specific parameters live here; all comparison/date/geometry
logic is in shared operators. The 3.0 -> 3.1 difference is exactly the
small-package threshold (100 mm -> 80 mm) per the published JSONs.

Rule-specific parameters carried by the plan (never by shared operators):

- ``material_number_pattern``: the material-number format regex (item 1).
- ``rule_quotes``: concise original rule-text fragments stored under stable
  keys so produced ``Evidence.rule_quote`` values quote the active version's
  own text.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Stable quote keys -> original rule-text fragments (B001 3.0 wording).
QUOTES_3_0: dict[str, str] = {
    "model_match": "型号经总则规定的型号归一化后，应与商品申报型号一致",
    "number_format": "材料编号应采用 QLAB- 后接 6 位十进制数字的形式，前缀必须大写",
    "same_label": "上述两个字段应出现在同一张标签上",
    "carrier_outer_package": "包装最长边不小于 100 毫米时，标签应印制或粘贴在外包装上",
    "attached_tag": "包装最长边小于 100 毫米时，可使用固定在包装上的附签；实拍图必须同时显示附签与该包装的连接关系",
    "tag_attachment": "不能仅凭单独附签照片确认符合",
    "cert_fields": "证明文件应具有“材料编号”“覆盖型号”“有效起始日”“有效截止日”四个字段",
    "cert_number_match": "材料编号应与包装或符合第 2 项要求的附签上的编号一致",
    "cert_coverage": "覆盖型号可以列出多个完整型号，必须包含申报型号；不接受通配符、系列简称或外观相似替代",
    "validity": "有效起始日不得晚于有效截止日，审核基准日期应位于该有效期间内，包含起止日",
    "conflict_label_number": "同一包装的多份未作废标签存在不同的“材料编号”值，或两份未标注作废的同号证明文件有效期不同，视为材料冲突，判为 ambiguous",
    "conflict_cert_period": "两份未标注作废的同号证明文件有效期不同，视为材料冲突，判为 ambiguous",
    "voided_only": "清楚标注“作废”的旧标签或旧文件不作为有效证据，也不能单独用于满足要求",
}

QUOTES_3_1: dict[str, str] = {
    key: value.replace("100 毫米", "80 毫米") if "毫米" in value else value
    for key, value in QUOTES_3_0.items()
}


class B001Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: Literal["B001"] = "B001"
    rule_version: str = Field(pattern=r"^\d+\.\d+$")
    # B001 item 2: 包装最长边小于阈值 -> 附签可用; 等于阈值 -> 不属于小包装.
    small_tag_threshold_mm: Decimal
    applicable_market: str = "Q"
    applicable_category: str = "桌面灯"
    applicable_power_mode: str = "可充电"
    # Item 1 material-number format; pattern lives in the plan, not in the
    # shared text operators (rule-ID independence).
    material_number_pattern: str = r"^QLAB-[0-9]{6}$"
    # Concise original rule-text fragments for Evidence.rule_quote, keyed by
    # stable quote keys (see QUOTES_3_0 / QUOTES_3_1).
    rule_quotes: dict[str, str] = Field(default_factory=lambda: dict(QUOTES_3_0))

    def quote(self, key: str) -> str | None:
        return self.rule_quotes.get(key)

    def fact_requirements(self) -> tuple[str, ...]:
        """Predicates this plan needs from observed entities."""
        return (
            "model",
            "material_number",
            "carrier",
            "void_marked",
            "attached_to_package",
            "covered_models",
            "valid_from",
            "valid_until",
        )


def plan_3_0() -> B001Plan:
    return B001Plan(
        rule_version="3.0",
        small_tag_threshold_mm=Decimal("100"),
        rule_quotes=dict(QUOTES_3_0),
    )


def plan_3_1() -> B001Plan:
    return B001Plan(
        rule_version="3.1",
        small_tag_threshold_mm=Decimal("80"),
        rule_quotes=dict(QUOTES_3_1),
    )
