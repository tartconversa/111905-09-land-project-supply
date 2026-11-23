"""精确运算工具。

来源契约要求 area_sqm 为十进制字符串、份额为分数（如 1/4）。
本模块统一用 fractions.Fraction 处理份额、decimal.Decimal 处理面积，
全程不使用二进制浮点，杜绝 0.30000000000000004 类误差与隐性比例分摊。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

# 面积统一保留 4 位小数；来源数据目前均为整数平方米
AREA_QUANT = Decimal("0.0001")


def parse_fraction(text: str | None) -> Fraction | None:
    """解析 '1/4'、'0.25'、'1' 等字符串为精确分数。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if "/" in s:
        numerator, denominator = s.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    return Fraction(s)


def fraction_str(value: Fraction) -> str:
    """分数规范化为 'p/q' 字符串（整数也保留分母 1，便于口径识别）。"""
    return f"{value.numerator}/{value.denominator}"


def parse_area(text: str | int | Decimal | None) -> Decimal | None:
    """面积字符串转 Decimal，不经过 float。"""
    if text is None:
        return None
    return Decimal(str(text))


def scale_area(area: Decimal, share: Fraction) -> Decimal:
    """按份额缩放面积，结果四位小数、四舍五入，全程精确运算。"""
    scaled = area * Decimal(share.numerator) / Decimal(share.denominator)
    return scaled.quantize(AREA_QUANT, rounding=ROUND_HALF_UP)


def row_hash(payload: dict) -> str:
    """对一行记录计算稳定哈希，用于增量去重与发布回查。"""
    import hashlib
    import json

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
