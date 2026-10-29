"""补读遗漏的付款标签或局部数字，并保留每个金额的识别依据。"""
import csv
import io
from pathlib import Path
import re
import tempfile

from PIL import Image

from imss.core.models import money, normalized
from imss.recognition.extract import image_pages, tesseract_path, IMAGE_EXTENSIONS, ocr_image
from imss.recognition.layout import visual_lines, evidence, contains_amount
from imss.recognition.fields import PAID, extract_order_amount
from imss.recognition.rules import clean_text
from imss.runtime.office import run_process
from imss.runtime.paths import tessdata_path


def refine_missing_amount(path, material, settings):
    """补读明确付款标签或支付/行程区域，不能从任意数字中猜总额。"""
    if material.kind == '电子发票':
        from imss.recognition.invoices import refine_invoice_fields
        return refine_invoice_fields(path, material, settings)
    if material.kind == '购买清单':
        return refine_list_total(path, material, settings)
    if (material.extension not in IMAGE_EXTENSIONS or material.kind != '购买记录'
            or material.amount or not material.layout
            or any(any(word in issue for word in ('冲突', '退款', '尚未付款')) for issue in material.issues)):
        return
    lines = visual_lines(material.layout)
    regions = []
    for line in lines:
        if line['page'] != 1:
            continue
        compact = normalized(line['text'])
        y = line['box'][1]
        if any(word in compact for word in ('当前状态', '自动扣款成功')):
            regions.append((.18, max(.08, y - .16), .85, max(.09, y - .008)))
        elif material.category == '行程' and ('全程' in compact or '费用说明' in compact):
            regions.append((.06, max(0, y - .075), .78, y))
    if not regions:
        return refine_order_payment(path, material, settings)
    values, boxes = set(), []
    with Image.open(path) as original:
        if getattr(original, 'n_frames', 1) != 1:
            return  # 多帧不把某一页的费用当成整份总额。
    pages = image_pages(path, settings)
    try:
        image = next(pages)
        try:
            for x1, y1, x2, y2 in regions[:2]:
                if y2 <= y1:
                    continue
                crop = image.crop((int(x1 * image.width), int(y1 * image.height),
                                   int(x2 * image.width), int(y2 * image.height))).convert('L')
                try:
                    factor = min(4, max(1, 1200 / crop.width), (settings.max_pixels / (crop.width * crop.height)) ** .5)
                    if factor > 1:
                        resized = crop.resize((int(crop.width * factor), int(crop.height * factor)), Image.Resampling.LANCZOS)
                        crop.close()
                        crop = resized
                    with tempfile.TemporaryDirectory(prefix='imss-amount-') as directory:
                        target = Path(directory) / 'amount.png'
                        crop.save(target)
                        args = [tesseract_path(settings), str(target), 'stdout', '-l', 'eng', '--psm', '6',
                                '--tessdata-dir', str(tessdata_path(settings)), '-c', 'tessedit_create_tsv=1',
                                '-c', 'tessedit_create_txt=0', '-c', 'tessedit_char_whitelist=0123456789.-']
                        result = run_process(args, settings.ocr_timeout)
                    if result.returncode:
                        continue
                    rows = csv.DictReader(io.StringIO(result.stdout.decode('utf-8', errors='replace')), delimiter='\t')
                    for row in rows:
                        text = row.get('text', '').strip()
                        # 小数点不可猜；不把 2170 自动改成21.70，也不从百分比截取数字。
                        if not re.fullmatch(r'-?\d+\.\d{2}', text):
                            continue
                        score = float(row.get('conf', '-1'))
                        if score < 40:
                            continue
                        value = money(text.lstrip('-'))
                        values.add(value)
                        left, top, width, height = (float(row[name]) for name in ('left', 'top', 'width', 'height'))
                        boxes.append(dict(text=text, confidence=score, page=1, engine='tesseract-numeric',
                            box=[x1 + left / crop.width * (x2 - x1), y1 + top / crop.height * (y2 - y1),
                                 x1 + (left + width) / crop.width * (x2 - x1),
                                 y1 + (top + height) / crop.height * (y2 - y1)]))
                finally:
                    crop.close()
        finally:
            image.close()
    finally:
        pages.close()
    if len(values) == 1:
        material.amount = values.pop()
        material.layout.extend(boxes)
        material.field_evidence['amount'] = evidence(material.amount, visual_lines(boxes), '支付/费用区域局部数字OCR')
        material.issues = [issue for issue in material.issues if issue != '未找到明确的含税合计或实付金额']
        material.issues.append('局部数字OCR提取金额，需核对是否实付及小数点；确认后才可生成')
    elif len(values) > 1:
        material.issues.append('局部数字OCR存在多个金额，未自动选择，请核对原件')


def refine_order_payment(path, material, settings):
    """首轮付款标签可能乱码；只补实付字段，不替换首轮商品或整页文字。"""
    with Image.open(path) as original:
        if getattr(original, 'n_frames', 1) != 1:
            return
    if any(block['page'] != 1 for block in material.layout):
        return
    pages = image_pages(path, settings)
    try:
        image = next(pages)
        try:
            result = ocr_image(image, settings, _psm=11)
        finally:
            image.close()
    finally:
        pages.close()
    text = clean_text(result[0])
    text = re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', text)
    text = re.sub(r'(?<=\d)\s*\.\s*(?=\d)', '.', text)
    if not re.search(PAID, text):
        return  # 不从商品单价、返现、日期或订单总额中猜实付款。
    value, warnings = extract_order_amount(text)
    if warnings:
        material.issues.extend(note for note in warnings if note not in material.issues)
    if not value or warnings or result[1]:
        return
    blocks = [dict(block, engine='tesseract-order-payment') for block in result.layout]
    lines = visual_lines(blocks)
    # 仅保存付款标签与金额的补读证据，避免把第二轮较差的商品文字混入首轮。
    matched = [line for line in lines if re.search(PAID, normalized(line['text']))
               or contains_amount(line['text'], value)]
    material.amount = value
    if value not in material.related_amounts:
        material.related_amounts.append(value)
    material.layout.extend(block for line in matched for block in line['blocks'])
    material.field_evidence['amount'] = evidence(value, matched, '订单实付标签与金额的补充OCR')
    material.issues = [issue for issue in material.issues if issue not in (
        '未找到明确的含税合计或实付金额', '金额字段无法完整读取，请核对原件',
        '实付字段存在但金额不明确，请核对原件')]


def refine_list_total(path, material, settings):
    """长清单底部常被整页 OCR 漏掉；只重读一次下半页，必须读到总额标签。"""
    from imss.recognition.tables import extract_list_amount, TOTAL_LABEL
    if (material.extension not in IMAGE_EXTENSIONS or material.amount
            or any('冲突' in issue for issue in material.issues)):
        return
    with Image.open(path) as original:
        if getattr(original, 'n_frames', 1) != 1:
            return
    pages = image_pages(path, settings)
    try:
        image = next(pages)
        try:
            top, bottom, right = int(image.height * .45), int(image.height * .85), int(image.width * .85)
            # 合计常位于正文左下，右下印章和底边空白会干扰整块文字分割。
            crop = image.crop((0, top, right, bottom))
            try:
                result = ocr_image(crop, settings, _psm=6)
            finally:
                crop.close()
            value, warnings = extract_list_amount(result[0])
            compact_text = re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', result[0])
            if not value or any('冲突' in warning or '未读清' in warning for warning in warnings) or not TOTAL_LABEL.search(compact_text):
                return
            boxes = [dict(block, engine='tesseract-list-total', box=[block['box'][0] * right / image.width,
                         top / image.height + block['box'][1] * (bottom - top) / image.height,
                         block['box'][2] * right / image.width, top / image.height + block['box'][3] * (bottom - top) / image.height])
                     for block in getattr(result, 'layout', [])]
        finally:
            image.close()
    finally:
        pages.close()
    material.amount = value
    material.layout.extend(boxes)
    material.field_evidence['amount'] = evidence(value, visual_lines(boxes), '清单底部带总额标签的局部OCR')
    material.issues = [issue for issue in material.issues if issue not in (
        '未找到明确的清单总额', '未读清清单总额，请核对合计行或金额列')]
    material.issues.append('清单总额来自底部局部OCR，请核对原件后确认')
    material.issues.extend(warnings)
