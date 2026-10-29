"""从提取文字中识别材料类型、商品与金额，不依赖原文件名猜测业务事实。

增加版式时先补匿名文本回归用例，再修改本模块规则；证据不充分的字段
留空并记录问题，由核对清单交给用户确认。
"""
import re
import unicodedata

from imss.core.models import Material, money, normalized, STANDALONE_SHEETS
from imss.recognition.tables import sheet_heading, list_heading, looks_like_list

from imss.recognition.fields import MONEY_PATTERN, extract_amount, extract_item, extract_order_ids
from imss.recognition.layout import enrich_fields
from imss.recognition.invoices import has_invoice_structure

PURCHASE_MARKERS = ("订单编号", "订单号", "实付款", "订单详情", "交易成功", "支付成功", "购买记录",
                    "交易单号", "付款时间", "支付时间", "费用详情", "行程详情", "实付", "行程已结束",
                    "呼叫返程", "账单详情", "自动扣款成功", "商户单号", "商品总价", "应付金额", "待付款")


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r", "\n")
    # Word 表格的制表符是单元格边界，不能与普通空格一起压掉。
    return "\n".join(re.sub(r"[ \u3000]+", " ", line).strip() for line in text.splitlines() if line.strip())


def classify(text: str) -> str:
    compact = normalized(text)
    # A declaration may mention invoices; its title takes precedence.
    head = normalized("\n".join(text.splitlines()[:4]))
    sheet, _ = sheet_heading(text)
    if sheet:
        return sheet
    for kind, words in (
        ("情况说明", ("情况说明",)),
        ("新闻稿", ("新闻稿",)),
        ("审查表", ("项目审查表", "项目审核表", "审查表", "项目审批表", "申报审批表")),
        ("承诺书", ("承诺书",)),
    ):
        if any(word in head for word in words):
            return kind
    if "发布者" in compact and "发布时间" in compact and "浏览次数" in compact:
        return "新闻稿"
    if "申报审批表" in compact[:250]:
        return "审查表"
    for kind, words in (("行程单", ("行程单", "itinerary")), ("运单明细", ("运单明细",))):
        if any(word in compact for word in words):
            return kind
    if list_heading(text):
        return "购买清单"
    # 订单里“查看电子发票/发票号码”的入口不能压过购买记录正文。
    purchase = any(word in compact for word in PURCHASE_MARKERS)
    invoice_title = any(re.match(r'^(?:电子发票|数电发票|增值税.*发票)', normalized(line))
                        for line in text.splitlines()[:5])
    if ('价税合计' in compact or invoice_title or has_invoice_structure(text)
            or (not purchase and any(word in compact for word in ('电子发票', '数电发票', '增值税', '发票号码')))):
        return "电子发票"
    if re.search(r"(?<!\d)\d{20}(?!\d)", text) and any(word in compact for word in ("社会信用", "征收率", "纳税人")):
        return "电子发票"
    if purchase:
        return "购买记录"
    if looks_like_list(text):
        return "购买清单"
    return "未知"


def recognize(material: Material) -> Material:
    """只根据正文提取业务字段；保留提取阶段警告供后续人工核对。"""
    text = clean_text(material.text)
    material.text = text
    material.kind = classify(text)
    # OCR often puts spaces between Chinese characters, but preserve word boundaries elsewhere.
    fields_text = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", text)
    fields_text = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", fields_text)
    material.title = text.splitlines()[0][:100] if text else ""
    material.order_ids = extract_order_ids(fields_text)
    # 付款账单也可能有20位交易号，不能把它当成发票号参与配对。
    invoice_text = fields_text if material.kind == '电子发票' else '\n'.join(
        match[1] for match in re.finditer(r'(?:发票号码|发票号)\s*[:：]?\s*(\d{20})(?!\d)', fields_text))
    material.invoice_ids = sorted(set(re.findall(r"(?<!\d)\d{20}(?!\d)", invoice_text)))
    compact = normalized(fields_text)
    material.category = ("行程" if any(word in compact for word in ("交通运输", "打车", "客运服务", "旅客运输", "滴滴出行", "高德打车", "行程已结束", "呼叫返程", "行程详情"))
                         else "工作餐" if any(word in compact for word in ("餐饮服务", "工作餐", "快餐"))
                         else "快递" if any(word in compact for word in ("收派服务", "运单", "顺丰速运")) else "物品")
    material.related_amounts = []
    for amount in re.findall(r"(?:[¥￥]\s*" + MONEY_PATTERN + r"|" + MONEY_PATTERN + r"\s*元)", fields_text):
        value = next((part for part in amount if part), "") if isinstance(amount, tuple) else amount
        try:
            value = money(value)
            if value not in material.related_amounts:
                material.related_amounts.append(value)
        except ValueError:
            pass
    if material.kind in STANDALONE_SHEETS:
        _, material.title = sheet_heading(text)
        material.item = material.title
        material.amount = ''  # 人员表只按标题独立导出，不需要采购金额或发票分组。
        return material
    if material.kind in ("审查表", "新闻稿", "情况说明"):
        if material.kind == "审查表":
            project = re.search(r"项目名称\s*[:：]?\s*([^\n]+)", fields_text)
            if project:
                material.title = project.group(1).strip(' “”、|')[:100]
            else:
                title = next((line for line in fields_text.splitlines() if '审批表' in line or '审查表' in line), '')
                material.title = title or material.title
        elif material.kind == '新闻稿':
            lines = fields_text.splitlines()
            for index, line in enumerate(lines):
                if '发布者' in line or '发布时间' in line:
                    if index and len(lines[index - 1]) >= 8:
                        material.title = lines[index - 1][:100]
                    break
        material.item = material.title
        return material
    if material.kind == "未知":
        material.issues.append("未识别材料类型")
        return material
    material.item, issues = extract_item(fields_text, material.kind)
    material.issues.extend(issues)
    material.amount, issues = extract_amount(fields_text, material.kind)
    material.issues.extend(issues)
    if material.kind in ("行程单", "运单明细"):
        material.category = "行程" if material.kind == "行程单" else "快递"
        material.item = "打车费用" if material.kind == "行程单" else "快递费用"
        material.issues = [issue for issue in material.issues if issue != "未找到明确的物品名称"]
        if material.kind == "行程单":
            # Itinerary table rows often show bare amounts without a currency
            # symbol. Include only a decimal at the end of a visual line.
            for value in re.findall(r"(?m)(?<![\d.])(\d+\.\d{1,2})\s*(?:元)?\s*$", fields_text):
                try:
                    amount = money(value)
                    if amount not in material.related_amounts:
                        material.related_amounts.append(amount)
                except ValueError:
                    pass
    elif material.kind == "电子发票" and material.category in ("行程", "快递"):
        material.item = "打车费用" if material.category == "行程" else "快递费用"
        material.issues = [issue for issue in material.issues if issue != "未找到明确的物品名称"]
    elif material.kind == "购买记录" and material.category in ("行程", "快递"):
        material.item = "打车费用" if material.category == "行程" else "快递费用"
        material.issues = [issue for issue in material.issues if issue != "未找到明确的物品名称"]
    enrich_fields(material)
    return material
