"""发票的独立税务特征与局部 OCR；版式只定位字段，不代替金额和品名证据。"""
from dataclasses import replace
import re

from PIL import Image, ImageChops

from imss.core.models import normalized, money


def invoice_features(text):
    """标题漏字时仍比较税号、税率、小写金额等；订单中的发票链接不够。"""
    compact = normalized(text)
    tax_identity = any(word in compact for word in ('社会信用', '纳税人', '识别号', '识别引'))
    tax_table = any(word in compact for word in ('税额', '税率', '征收率'))
    gross = '价税合计' in compact or '小写' in compact
    metadata = '开票日期' in compact or '发票号码' in compact
    partial_title = any(re.match(r'^(?:电子发|增值税)', normalized(line))
                        for line in text.splitlines()[:16])
    # 18 位税号与 20 位发票/交易号分开；只合并同一行的数字和字母间空格。
    ids = set()
    for line in text.splitlines():
        line = re.sub(r'(?<=[A-Z0-9]) +(?=[A-Z0-9])', '', line.upper())
        ids.update(re.findall(r'(?<![A-Z0-9])[0-9][A-Z0-9]{17}(?![A-Z0-9])', line))
    return dict(tax_identity=tax_identity, tax_table=tax_table, gross=gross,
                metadata=metadata, partial_title=partial_title, tax_ids=len(ids))


def has_invoice_structure(text):
    f = invoice_features(text)
    return ((f['gross'] and (f['tax_identity'] or f['tax_table'] or f['tax_ids'] >= 1))
            or (f['tax_identity'] and f['tax_table'] and f['metadata'])
            or (f['partial_title'] and f['tax_ids'] >= 2))


def invoice_regions(image):
    """定位红色网格发票的明细区与下一行价税合计区；不按文件名猜坐标。"""
    red, green, blue = image.split()
    mask = ImageChops.darker(ImageChops.subtract(red, green), ImageChops.subtract(red, blue))
    mask = mask.point(lambda value: 1 if value > 20 else 0)
    raw = mask.tobytes()
    rows = [y for y in range(image.height)
            if sum(raw[y * image.width:(y + 1) * image.width]) > image.width * .65]
    clusters = []
    for y in rows:
        if not clusters or y > clusters[-1][-1] + 2:
            clusters.append([y])
        else:
            clusters[-1].append(y)
    lines = [round(sum(group) / len(group)) for group in clusters]
    if len(lines) < 4:
        return {}
    gaps = [(lines[i + 1] - lines[i], i) for i in range(len(lines) - 2)
            if lines[i] > image.height * .25]
    if not gaps:
        return {}
    gap, index = max(gaps)
    top, bottom = lines[index + 1] + 2, lines[index + 2] - 1
    if not (.15 * image.height < gap < .65 * image.height and 3 <= bottom - top < .15 * image.height):
        return {}
    return dict(amount=(int(image.width * .60), top, int(image.width * .98), bottom),
                item=(0, lines[index] + int(gap * .1), int(image.width * .25), lines[index + 1] - 2))


def local_gross(text):
    """必须有货币符号及两位小数；没有小数点的 929 不能猜成 9.29。"""
    values, warnings = set(), []
    symbols = list(re.finditer(r'[¥￥]|Y(?=\s*\d)', text))
    matches = list(re.finditer(r'[¥￥Y]\s*(\d+(?:,\d{3})*)\s*([.,])\s*(\d{2})(?![\d.,%])', text))
    if len(symbols) != len(matches):
        return '', ['价税合计区域还有未读清的金额，请核对原件']
    for match in matches:
        if re.search(r'[-−]\s*$', text[:match.start()]):
            continue
        try:
            values.add(money(match[1].replace(',', '') + '.' + match[3]))
            if match[2] == ',':
                warnings.append('价税合计小数点被OCR读成逗号，请核对原件')
        except ValueError:
            pass
    if len(values) == 1:
        return values.pop(), list(dict.fromkeys(warnings))
    return '', ['价税合计区域存在多个金额，请核对原件'] if values else []


def refine_invoice_fields(path, material, settings):
    from imss.recognition.extract import image_pages, ocr_image, IMAGE_EXTENSIONS
    from imss.recognition.fields import extract_item
    from imss.recognition.layout import evidence, visual_lines
    if (material.extension not in IMAGE_EXTENSIONS or (material.amount and material.item)
            or any('冲突' in issue for issue in material.issues)):
        return
    pages = image_pages(path, settings)
    try:
        image = next(pages)
        try:
            # 单帧发票才按一张表格定位，避免将某一页金额当整份总额。
            with Image.open(path) as original:
                if getattr(original, 'n_frames', 1) != 1:
                    return
            regions = invoice_regions(image)
            for field, box in regions.items():
                if getattr(material, field):
                    continue
                crop = image.crop(box)
                try:
                    scale = 3 if image.width < 1000 else 1
                    enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.BICUBIC)
                    try:
                        result = ocr_image(enlarged, replace(settings, languages='eng') if field == 'amount' else settings,
                                           _psm=7 if field == 'amount' else 6)
                    finally:
                        enlarged.close()
                    value, warnings = local_gross(result[0]) if field == 'amount' else extract_item(result[0], '电子发票')
                    if field == 'amount' and not value and scale > 1:
                        # 放大有时会抹掉小数点，只重试一次原裁图，不根据发票或文件名补点。
                        result = ocr_image(crop, replace(settings, languages='eng'), _psm=7)
                        value, warnings = local_gross(result[0])
                finally:
                    crop.close()
                if field == 'amount' and any('多个金额' in warning for warning in warnings):
                    material.issues.extend(warnings)
                    continue
                if not value or (field == 'item' and (warnings or result[1])):
                    continue
                blocks = [dict(block, engine='tesseract-invoice-' + field,
                    box=[(box[0] + block['box'][0] * (box[2] - box[0])) / image.width,
                         (box[1] + block['box'][1] * (box[3] - box[1])) / image.height,
                         (box[0] + block['box'][2] * (box[2] - box[0])) / image.width,
                         (box[1] + block['box'][3] * (box[3] - box[1])) / image.height])
                    for block in getattr(result, 'layout', [])]
                setattr(material, field, value)
                material.layout.extend(blocks)
                material.field_evidence[field] = evidence(value, visual_lines(blocks), '发票表格定位后的局部OCR')
                missing = '未找到明确的含税合计或实付金额' if field == 'amount' else '未找到明确的物品名称'
                material.issues = [issue for issue in material.issues if issue != missing]
                material.issues.extend(warnings)
                material.issues.append('发票字段来自局部OCR，请核对品名、价税合计及小数点后确认')
        finally:
            image.close()
    finally:
        pages.close()
