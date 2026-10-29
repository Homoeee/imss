"""标题与清单表格规则：人员表不要求金额，采购明细只读取有依据的总额。"""
import re
from decimal import Decimal

from imss.core.models import money, normalized

SHEET_WORDS = {
    '签领表': ('签领表', '签领单', '签收表', '签字表', '领取登记表', '用餐人员签到'),
    '签到表': ('签到表', '签到簿', '签到册', '签到名单', '签到登记表'),
}
LIST_TITLE = re.compile(r'(?:购买|采购|购货|购物|销售|销货|费用|物品|商品|物资|用品|餐费|结算|制作|支出).*(?:清单|明细表)$|活动用品明细$')
TOTAL_LABEL = re.compile(r'(?:以上)?(?:价税合计|合计金额|金额合计|总金额|总计|总额|总价|合计)')
NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?'


def text_lines(text):
    return [re.sub(r'(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])', '', line).strip()
            for line in text.splitlines() if line.strip()]


def sheet_heading(text):
    """只认开头的表名，避免把发票备注中的“请附签到表”当成标题。"""
    lines = text_lines(text)
    for index, line in enumerate(lines[:8]):
        compact = normalized(line)
        if re.search(r'请|需提供|需附|附件要求|备注|发票|购买清单|商品名称|物品名称|商品说明', compact):
            continue
        for kind, words in SHEET_WORDS.items():
            if not any(compact.endswith(word) for word in words):
                continue
            title = line
            if compact in words and index:
                previous = lines[index - 1]
                if (not re.search(r'^(?:附件|第?\d+页|姓名|序号|学号)', previous)
                        and len(normalized(previous)) >= 4):
                    title = previous + line
            return kind, title[:100]
    return '', ''


def list_heading(text):
    for line in text_lines(text)[:8]:
        if LIST_TITLE.search(normalized(line)) and not re.search(
                r'请|需附|备注|详见|见附件|商品名称|物品名称|商品说明|货物名称', line):
            return line[:100]
    return ''


def looks_like_list(text):
    """无明确表名时，必须同时有品名列、计价列和总额标签。"""
    compact = normalized(text)
    return (any(word in compact for word in ('品名', '商品名称', '物品名称', '项目名称'))
            and any(word in compact for word in ('数量', '单价'))
            and any(word in compact for word in ('金额', '总价'))
            and bool(TOTAL_LABEL.search(compact)))


def _columns(line):
    return [part.strip() for part in re.split(r'\t|[|｜]| {2,}', line)]


def _amount(text):
    text = re.sub(r'^(?:\(小写\)|小写|人民币|RMB|CNY)\s*', '', text.strip(), flags=re.I)
    text = text.strip(' :：|()（）')
    match = re.fullmatch(r'[¥￥]?\s*(' + NUMBER + r')\s*(?:元|圆)?(?:\s*[(（]含税[)）])?', text)
    if not match:
        return None
    try:
        return money(match[1])
    except ValueError:
        return None


def extract_list_amount(text):
    """总额与数量同一行时，按表头的金额列读取；不取任意最大值或行末单价。"""
    lines = text_lines(text)
    values, unreadable, notes = set(), False, []
    amount_column = None
    row_amounts, table_incomplete, table_closed = [], False, False
    for index, line in enumerate(lines):
        cells = _columns(line)
        header = [i for i, cell in enumerate(cells) if re.fullmatch(
            r'(?:金额|总价|总金额|金额小计|小计)(?:\s*[(（]?元[)）]?)?', cell)]
        if header and len(cells) > 1 and any(re.search(r'数量|单价|品名|名称|份数', c) for c in cells):
            amount_column = header[-1]
            table_closed = False
            continue
        if TOTAL_LABEL.search(line):
            table_closed = True
        if not table_closed and amount_column is not None and len(cells) > 1:
            value = _amount(cells[amount_column]) if amount_column < len(cells) else None
            if value:
                row_amounts.append(value)
            elif any(re.search(r'\d', cell) for cell in cells):
                table_incomplete = True
        for match in TOTAL_LABEL.finditer(line):
            if re.search(r'本页\s*$|分页\s*$', line[:match.start()]):
                continue  # 页小计不能覆盖整份总额。
            if match[0] == '总价' and line[:match.start()].strip(' :：|\t'):
                continue  # 明细行“某物品总价”不是整份清单的总额。
            tail = line[match.end():]
            tail = re.sub(r'^\s*[(（](?:元|人民币)[)）]', '', tail)
            if re.search(r'小写', tail):
                tail = re.split(r'小写\s*[)）]?', tail)[-1]
            value = _amount(tail)
            if not value:
                # 印章或备注可能紧接“元”被 OCR 拼入；保留额外文字提示，不截取第二个数字。
                decorated = re.fullmatch(r'\s*[:：¥￥ ]*(' + NUMBER + r')\s*元([^\d¥￥]{1,12})', tail)
                if decorated:
                    value = _amount(decorated[1])
                    notes.append('清单总额后有额外文字，请核对原件')
            if not value and amount_column is not None and amount_column < len(cells):
                value = _amount(cells[amount_column])
            if not value and not tail.strip(' :：|\t') and index + 1 < len(lines):
                value = _amount(lines[index + 1])
            if value:
                values.add(value)
            else:
                unreadable = True
    if len(values) > 1:
        return '', ['清单总额冲突: ' + '、'.join(sorted(values))]
    if values:
        return values.pop(), notes + (['清单还有未读清的总额字段，请核对原件'] if unreadable else [])
    if unreadable:
        if row_amounts and not table_incomplete:
            total = format(sum(Decimal(value) for value in row_amounts), '.2f')
            return total, ['合计栏未填写或未读清，按表格金额列汇总，请核对总额后确认']
        return '', ['未读清清单总额，请核对合计行或金额列']
    # 无标签的末尾纯金额可以提出建议，但不能把最后一个明细行的单价当总额。
    if lines and (re.search(r'[¥￥元圆.]', lines[-1])) and (value := _amount(lines[-1])):
        return value, ['清单末尾金额缺少合计标签，请核对总额']
    return '', ['未找到明确的清单总额']


def extract_list_item(text):
    """清单允许多种物品，以清单标题概括，不强行将第一项当成整笔品名。"""
    title = list_heading(text)
    if title:
        return title, []
    lines = text_lines(text)
    first = lines[0] if lines else ''
    if (len(normalized(first)) >= 4 and not re.search(r'序号|数量|单价|金额|品名|名称', first)
            and not re.search(r'\d', first)):
        return first[:100], []
    return '采购物品清单', []
