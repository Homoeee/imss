"""把文字框还原成视觉行，用字段邻接关系补全识别并留下可追溯证据。

置信度是 OCR 引擎的原始分数，不代表金额正确概率。坐标只支持提取，
不能用附近任意数字替代实付；多个明确支付值冲突时仍交给人工核对。
"""
from datetime import date
import re

from imss.core.models import money, normalized
from imss.recognition.fields import extract_amount, extract_item


def visual_lines(blocks, column_separator=' '):
    lines = []
    for block in sorted(blocks, key=lambda b: (b['page'], b['box'][1], b['box'][0])):
        x1, y1, x2, y2 = block['box']
        cy, height = (y1 + y2) / 2, y2 - y1
        target = next((line for line in reversed(lines[-12:]) if line['page'] == block['page']
                       and abs(line['cy'] - cy) <= min(line['height'], height) * .5), None)
        if target is None:
            target = dict(page=block['page'], cy=cy, height=height, blocks=[])
            lines.append(target)
        target['blocks'].append(block)
    result = []
    for line in lines:
        words = sorted(line['blocks'], key=lambda b: b['box'][0])
        # 汉字框不加空格；分开的英文单词仍保留边界。
        text, last = '', None
        for word in words:
            if last and word['box'][0] - last['box'][2] > line['height'] * .8:
                text += column_separator
            text += word['text']
            last = word
        box = [min(b['box'][0] for b in words), min(b['box'][1] for b in words),
               max(b['box'][2] for b in words), max(b['box'][3] for b in words)]
        result.append(dict(text=text, page=line['page'], box=box, blocks=words))
    return sorted(result, key=lambda line: (line['page'], line['box'][1]))


def evidence(value, lines, method):
    return dict(value=value, method=method,
                regions=[dict(page=line['page'], box=line['box'], text=line['text'],
                              confidence=min((b['confidence'] for b in line['blocks']
                                              if b.get('confidence') is not None), default=None)) for line in lines])


def contains_amount(text, value):
    """按完整数字比较，避免把订单号中的一段数字当成金额坐标。"""
    for number in re.findall(r'(?<![\d.])\d+(?:\.\d{1,2})?(?![\d.])', text):
        try:
            if money(number) == value:
                return True
        except ValueError:
            continue
    return False


def list_page_text(lines):
    """按表头的横坐标补回空列；合计行未填写数量时金额仍落在金额列。"""
    output, centers = [], []
    for line in lines:
        cells = []
        for block in sorted(line['blocks'], key=lambda b: b['box'][0]):
            text = block['text'].strip('|｜_')
            if not text:
                continue  # OCR 会把竖向表格边框读成独立字符。
            if not cells or block['box'][0] - cells[-1]['right'] > (block['box'][3] - block['box'][1]) * .8:
                cells.append(dict(text=text, left=block['box'][0], right=block['box'][2]))
            else:
                cells[-1]['text'] += text
                cells[-1]['right'] = block['box'][2]
        for index in reversed(range(1, len(cells))):
            if re.fullmatch(r'[(（]?元[)）]?', cells[index]['text']):
                cells[index - 1]['text'] += cells[index]['text']
                cells[index - 1]['right'] = cells[index]['right']
                cells.pop(index)
        if (any(re.search(r'金额|总价', cell['text']) for cell in cells)
                and any(re.search(r'数量|单价|份数', cell['text']) for cell in cells)):
            centers = [(c['left'] + c['right']) / 2 for c in cells]
        if centers and len(cells) > 1:
            row = [''] * len(centers)
            for cell in cells:
                cx = (cell['left'] + cell['right']) / 2
                column = min(range(len(centers)), key=lambda i: abs(cx - centers[i]))
                row[column] = (row[column] + ' ' + cell['text']).strip()
            output.append('\t'.join(row))
        else:
            output.append(line['text'])
    return '\n'.join(output)


def enrich_fields(material):
    """保留正文提取，只有无冲突且位置关系明确时才补字段。"""
    lines = visual_lines(material.layout, '\t' if material.kind == '购买清单' else ' ') if material.layout else []
    layout_text = '\n'.join(line['text'] for line in lines)
    layout_text = re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', layout_text)
    material.field_evidence = {}
    if lines and material.kind in ('购买记录', '电子发票', '购买清单'):
        # 金额标签和值必须在同页；跨页拼接会把下一页的单价误当成实付。
        pages = {line['page'] for line in lines}
        page_texts = []
        for page in sorted(pages):
            page_lines = [line for line in lines if line['page'] == page]
            page_text = list_page_text(page_lines) if material.kind == '购买清单' else '\n'.join(line['text'] for line in page_lines)
            page_texts.append(re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', page_text))
        results = [extract_amount(text, material.kind) for text in page_texts]
        amounts = {value for value, warnings in results if value and (not warnings or material.kind == '购买清单')}
        amount = next(iter(amounts)) if len(amounts) == 1 else ''
        warnings = [warning for value, notes in results if value == amount for warning in notes] if amount else ['页面内缺少唯一实付字段']
        if len(pages) > 1 and material.amount and material.amount not in amounts:
            material.amount = ''
            material.issues.append('跨页金额证据不完整或冲突，请逐页核对总额')
        if amount and (not warnings or material.kind == '购买清单') and not material.amount:
            material.amount = amount
            material.issues = [issue for issue in material.issues
                               if issue not in ('未找到明确的含税合计或实付金额', '未找到明确的清单总额',
                                                '未读清清单总额，请核对合计行或金额列')]
            material.issues.extend(warning for warning in warnings if warning not in material.issues)
        elif amount and material.amount and amount != material.amount:
            material.issues.append('坐标提取金额与正文提取金额冲突，请核对原件')
        if not material.item:
            items = {value for text in page_texts for value, warnings in [extract_item(text, material.kind)]
                     if value and not warnings}
            if len(items) == 1:
                item = next(iter(items))
                material.item = item
                material.issues = [issue for issue in material.issues if issue != '未找到明确的物品名称']
    for field in ('item', 'amount'):
        value = getattr(material, field)
        if field == 'amount':
            matched = [line for line in lines if value and contains_amount(line['text'], value)]
        else:
            matched = [line for line in lines if normalized(value) and normalized(value) in normalized(line['text'])]
        if value:
            material.field_evidence[field] = evidence(value, matched, '视觉行与字段规则' if matched else '正文规则')
    text = layout_text or material.text
    # 只接受有标签的商家与日期，不从文件名或页面任意公司、任意日期猜测。
    merchant = re.search(r'(?:店铺名称|商家名称|收款方|商户名称|商户全称|销售方名称)\s*[:：]?\s*([^\n]+)', text)
    if merchant:
        material.merchant = merchant.group(1).strip()[:100]
        material.field_evidence['merchant'] = evidence(material.merchant,
            [line for line in lines if material.merchant in line['text']], '商家标签')
    dt = re.search(r'(?:付款时间|支付时间|下单时间|订单时间|开票日期)\s*[:：]?\s*(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})', text)
    if dt:
        try:
            material.transaction_date = date(*map(int, dt.groups())).isoformat()
            material.field_evidence['transaction_date'] = evidence(material.transaction_date,
                [line for line in lines if dt.group(1) in line['text']], '带标签日期')
        except ValueError:
            pass
