"""业务数据与公共校验；本层不访问 OCR、Office 或输出文件。"""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import unicodedata

KINDS = ("电子发票", "购买记录", "行程单", "运单明细", "购买清单", "签领表", "签到表", "承诺书", "审查表", "新闻稿", "情况说明", "未知")
STANDALONE_SHEETS = ("签领表", "签到表")
SPECIALS = ("审查表", "新闻稿", "情况说明")
MATERIALS = ("电子发票", "行程单", "运单明细", "购买记录", "购买清单", "签领表", "承诺书")


def money(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace(",", "").strip()
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"金额无效: {value}") from exc
    if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999.99"):
        raise ValueError(f"金额超出范围: {value}")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"金额最多两位小数: {value}")
    return format(amount, ".2f")


def safe_name(value: str, limit: int = 64) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .")
    if not value:
        return "未命名"
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", value).lower())


@dataclass
class Material:
    """一份原件。source 始终是相对输入目录的路径，digest 用于防止核对错原件。"""
    source: str
    digest: str
    extension: str
    kind: str = "未知"
    item: str = ""
    amount: str = ""
    order_ids: list[str] = field(default_factory=list)
    invoice_ids: list[str] = field(default_factory=list)
    related_amounts: list[str] = field(default_factory=list)
    category: str = ""
    group: str = ""
    title: str = ""
    text: str = ""
    issues: list[str] = field(default_factory=list)
    duplicate_of: str = ""
    archived: str = ""
    match_basis: str = ""  # amount_only 表示仅凭金额提出配对，必须人工确认。
    confirmed: bool = False
    # 坐标统一为页面左上角原点的 0..1 比例；不依赖 OCR 放大倍数。
    layout: list[dict] = field(default_factory=list)
    field_evidence: dict = field(default_factory=dict)
    merchant: str = ""
    transaction_date: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Bundle:
    """一笔报销：必须有且只有一张发票，并通过名称、金额与附件完整性校验。"""
    key: str
    item: str
    amount: str
    materials: list[Material]
    issues: list[str] = field(default_factory=list)


@dataclass
class Settings:
    tesseract: str = ""
    tessdata: str = ""
    languages: str = "chi_sim+eng"
    soffice: str = ""
    office_backend: str = "libreoffice"
    dpi: int = 180
    ocr_timeout: int = 120
    office_timeout: int = 180
    max_pages: int = 100
    max_pixels: int = 40_000_000
    ocr_min_chars: int = 20
    workers: int = 2

    @classmethod
    def from_dict(cls, values):
        unknown = set(values) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")
        result = cls(**values)
        for key in ("dpi", "ocr_timeout", "office_timeout", "max_pages", "max_pixels", "ocr_min_chars", "workers"):
            if type(getattr(result, key)) is not int or getattr(result, key) <= 0:
                raise ValueError(f"{key} 必须是正整数")
        if result.workers > 8:
            raise ValueError("workers 最多为 8")
        for key in ("tesseract", "tessdata", "languages", "soffice", "office_backend"):
            if not isinstance(getattr(result, key), str):
                raise ValueError(f"{key} 必须是字符串")
        if result.office_backend not in ("auto", "libreoffice"):
            raise ValueError("office_backend 应为 libreoffice 或 auto，均使用项目内 LibreOffice")
        return result


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
