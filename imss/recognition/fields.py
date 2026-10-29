"""正文中的商品、金额和编号提取，不读取文件名或对照答案。

订单页通常没有“商品名称”标签，因此允许从价格/数量附近识别商品标题。
金额必须能解释为实际付款；优惠、多笔金额和数字断裂不能静默放行。
"""
import re

from imss.core.models import money, normalized

MONEY_PATTERN = r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)(?![\d.,])"
NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?(?![\d.,%])"
# Y 是 OCR 对人民币符号的常见误读，只在独立符号位置接受，不匹配商品型号。
CURRENCY_SIGN = r"(?:[¥￥]|(?<![A-Za-z])Y(?=\s*\d))"
CURRENCY = re.compile(CURRENCY_SIGN + r"\s*(?P<amount>" + NUMBER + r")")
PAID = r"(?:实付款|实付金额|实际支付|实际付款|合计支付|支付金额|付款金额|实付|已支付金额|付款合计)"
TOTAL = r"(?:订单总额|订单金额|应付金额|应付款|合计)"
REFUND = re.compile(r"(?:退款成功|已退款|部分退款|退款中|退款金额|退还金额|已退金额)")
DISCOUNT = re.compile(r"(?:共减(?:半)?|已减|已优惠|优惠金额|优惠(?!后)|立减|已抵|节省|减免|抵扣)\s*[:：¥￥A-Za-z]*" + NUMBER)
ITEM_LABEL = re.compile(r"(?:物品名称|商品名称|货物名称|项目名称|品名|商品说明|商品标题|宝贝名称)\s*[:：]?\s*")
# 此处列的是界面字段和规格起始词，不是商品词典；不按预设品名猜商品。
UI_LINE = re.compile(r"(?:订单信息|订单编号|订单号|订单详情|交易成功|支付成功|付款成功|支付时间|付款时间|付款方式|支付方式|商户|账单|收单机构|客服|好评|满意度|进店|旗舰店|专营店|专卖店|自营店|服务保障|收货|签收|送至|地址|物流|退货|退款|售后|加入购物车|再买|评价|优惠|运费|价保|合计|实付|总价|单价|直降|金额|规格|型号|税率|征收率|税额|保障|联系电话|手机号码|推荐|猜你喜欢|为你推荐|相似商品|复制|查看)")
VARIANT = re.compile(r"^(?:规格|型号|款式|颜色|尺寸|套餐|数量|已选|默认|均码|标准款|升级款|米白色|白色|黑色|金色|银色|蓝色|红色|黄色|绿色|粉色|紫色|杏白色|\d+(?:[-~]\d+)?\s*(?:cm|厘米|毫米|毫升|ml|升|码|寸))", re.I)
QUANTITY = re.compile(r"(?:[xX×]\s*\d+|数量\s*[:：]?\s*\d+)\s*$")


def _as_money(value):
    try:
        return money(value)
    except ValueError:
        return None


def _money_in_segment(segment):
    """在一个付款字段内选金额；货币符号优先，排除明确优惠与负数减免。"""
    values, issues = set(), []
    discounts = [match.span() for match in DISCOUNT.finditer(segment)]
    broken = list(re.finditer(CURRENCY_SIGN + r"\s*(\d+)\s+(\d{2})(?=\s*(?:元|[~vV>]|$))", segment))
    for match in broken:
        if (any(left <= match.start() < right for left, right in discounts)
                or re.search(r'[-−]\s*$', segment[:match.start()])):
            continue
        value = _as_money(match[1] + '.' + match[2])
        if value:
            values.add(value)
            issues.append('付款金额数字被空格断开，疑似缺失小数点，请核对原件')
    for match in CURRENCY.finditer(segment):
        if any(left <= match.start() < right for left, right in discounts):
            continue
        if any(match.start() == part.start() for part in broken):
            continue
        if re.search(r'[-−]\s*$', segment[:match.start()]):
            continue
        # 不把“¥49 34”截成49，也不从带百分号/过多小数位的数字中截取金额。
        if re.match(r'\s+\d', segment[match.end():]):
            issues.append('付款金额数字断裂或存在多个相邻数字，请核对原件')
            continue
        value = _as_money(match['amount'])
        if value:
            values.add(value)
    if values:
        consumed = discounts + [match.span() for match in CURRENCY.finditer(segment)] + [match.span() for match in broken]
        if any(not any(left <= match.start() < right for left, right in consumed)
               for match in re.finditer(r'(?<![\d.,])' + NUMBER, segment)):
            issues.append('付款字段还有未解释的数字，请核对优惠与实际支付金额')
        return values, issues
    # 没有货币符号时只接受整个字段为一个金额；不跨过中文字段去抓日期或优惠。
    bare = re.fullmatch(r"\s*[:：()）(¥￥A-Za-z]*\s*(" + NUMBER + r")\s*(?:元)?\s*[~vV>]*\s*", segment)
    if bare and (value := _as_money(bare[1])):
        values.add(value)
    return values, issues


def _labelled_values(lines, label):
    values, issues, found = set(), [], False
    for index, line in enumerate(lines):
        matches = list(re.finditer(label, line))
        for position, match in enumerate(matches):
            found = True
            end = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            segment = line[match.end():end]
            # 下一个金额字段是边界，禁止“实付款\n运费5元”读成实付5元。
            pieces = re.split(r'退款金额|运费|商品总价|订单总额|应付金额|支付时间|订单号', segment)
            segment = pieces[0]
            local, warnings = _money_in_segment(segment)
            if not local and len(pieces) == 1 and not re.search(r'\d', segment):
                # 上下两行排版：只接受相邻的纯金额行，不能跳过其他字段。
                if index + 1 < len(lines):
                    local, warnings = _money_in_segment(lines[index + 1])
                    if UI_LINE.search(lines[index + 1]):
                        local = set()
            # 也支持“¥35.80 实付款”和“¥35.80\n实付款”。
            if not local and len(pieces) == 1 and not segment.strip(' :：~vV>'):
                prefix = line[:match.start()]
                if not prefix.strip() and index:
                    prefix = lines[index - 1]
                if re.fullmatch(r'\s*[¥￥]?\s*' + NUMBER + r'\s*(?:元)?\s*', prefix):
                    local, warnings = _money_in_segment(prefix)
            values.update(local)
            issues.extend(warnings)
            if not local:
                issues.append('金额字段无法完整读取，请核对原件')
    return values, list(dict.fromkeys(issues)), found


def extract_order_amount(text):
    """同级实付标签一起比较，不能因正则顺序不同忽略另一个冲突金额。"""
    lines = text.splitlines()
    status = ['存在退款记录，请核对实付、退款及实际报销金额'] if REFUND.search(text) else []
    unpaid = any(re.fullmatch(r'(?:订单状态\s*[:：]?\s*)?(?:待付款|等待付款|待支付|交易关闭|订单已取消|已取消)', line) for line in lines)
    if unpaid:
        status.append('订单尚未付款或已经关闭，不能作为已付款凭证')
    values, warnings, paid_found = _labelled_values(lines, PAID)
    if len(values) == 1:
        return values.pop(), status + warnings
    if len(values) > 1:
        return '', status + warnings + ['实付金额冲突: ' + ', '.join(sorted(values))]
    if paid_found:
        # 实付字段存在但读不清时不能降级采用原价或应付款。
        return '', status + warnings + ['实付字段存在但金额不明确，请核对原件']
    # 微信/支付宝账单常把支出金额单独放在顶部，没有“实付”标签。
    bill = any(word in text for word in ('账单详情', '账单服务', '账单管理', '自动扣款成功', '交易详情'))
    success = any(word in text for word in ('支付成功', '扣款成功', '交易成功', '付款成功'))
    if bill and success and not status:
        head = re.split(r'支付时间|付款时间|支付方式|付款方式|商户全称|商品说明|账单管理', text)[0]
        amounts = set()
        for line in head.splitlines():
            match = re.fullmatch(r'\s*[-−]?\s*[¥￥]?\s*(\d+\.\d{1,2})\s*(?:元)?\s*', line)
            if match and (value := _as_money(match[1])):
                amounts.add(value)
        if len(amounts) == 1:
            return amounts.pop(), []
        if len(amounts) > 1:
            return '', ['账单存在多个支出金额，请核对原件']
    # 已结束的叫车页面可展示费用但未必已付款，始终附带核对提示。
    # 只看以货币符号开头的费用行，不从里程、时间、司机评分中挑数字。
    if '行程已结束' in text and any(word in text for word in ('全程', '呼叫返程')):
        values, warnings = set(), []
        for line in lines:
            if re.match(r'^' + CURRENCY_SIGN, line):
                local, issues = _money_in_segment(line)
                values.update(local)
                warnings.extend(issues)
        if len(values) == 1:
            return values.pop(), status + warnings + ['仅识别到行程页面费用，请核对是否实际支付金额']
        if len(values) > 1:
            return '', status + ['行程页面存在多个费用金额，请核对原件']
    values, warnings, _ = _labelled_values(lines, TOTAL)
    if len(values) == 1:
        return values.pop(), status + warnings + ['仅识别到订单总额或应付金额，请核对实际支付金额']
    if len(values) > 1:
        return '', status + ['订单金额冲突: ' + ', '.join(sorted(values))]
    # 保留明确“金额”字段，不能把“优惠金额”“退款金额”降级成付款。
    values, warnings, _ = _labelled_values(lines, r'(?<![\u4e00-\u9fff])金额\s*[:：]')
    if len(values) == 1:
        return values.pop(), status + warnings
    return '', status + ['未找到明确的含税合计或实付金额']


def extract_amount(text: str, kind: str):
    """订单付款与发票含税合计分别处理，避免升级订单规则影响发票税额。"""
    if kind == '购买记录':
        return extract_order_amount(text)
    if kind == '购买清单':
        from imss.recognition.tables import extract_list_amount
        return extract_list_amount(text)
    labels = ([r'价\s*税\s*合\s*计.{0,35}?(?:小\s*写|[¥￥])', r'小\s*写[)）]?', r'价\s*税\s*合\s*计']
              if kind == '电子发票' else [r'合\s*计', r'发票总金额\s*[(（]元[)）]', r'行程总金额', r'总\s*额'])
    for label in labels:
        values = {_as_money(match[1]) for match in re.finditer(label + r'\s*[:：()）\s¥￥元A-Za-z]*' + MONEY_PATTERN, text)} - {None}
        if len(values) == 1:
            return values.pop(), []
        if len(values) > 1:
            return '', ['金额冲突: ' + ', '.join(sorted(values))]
    return '', ['未找到明确的含税合计或实付金额']


def _title(value):
    value = re.split(r'[¥￥]|(?<![A-Za-z])Y(?=\d)|\s+(?:规格|数量|单价|金额)\s*[:：]?|[|\t]', value)[0]
    value = re.sub(r'\s*[xX×]\s*\d+\s*$', '', value)
    return value.strip(' *<>《》~、"“”')[:120]


def _plausible_title(value):
    return (len(normalized(value)) >= 4 and len(re.findall(r'[\u4e00-\u9fff]', value)) >= 2
            and not UI_LINE.search(value) and not VARIANT.search(value)
            and not re.search(r'\d{6,}|\*{3,}|\d{1,2}:\d{2}', value))


def extract_item(text: str, kind: str = ''):
    """先用显式品名，订单再用价格与数量锚点；多商品不能随意取第一件。"""
    if kind == '购买清单':
        from imss.recognition.tables import extract_list_item
        return extract_list_item(text)
    lines = text.splitlines()
    values = []
    for index, line in enumerate(lines):
        for match in ITEM_LABEL.finditer(line):
            value = line[match.end():]
            if not value.strip() and index + 1 < len(lines):
                value = lines[index + 1]
            value = _title(value)
            if (len(normalized(value)) >= 2 and not UI_LINE.search(value)
                    and not VARIANT.search(value) and value not in values):
                values.append(value)
    if not values:
        for match in re.finditer(r'\*([^*\n]{1,30})\*([^\n]+)', text):
            category, raw_value = match.groups()
            value = re.split(r'\s|[|]', raw_value.strip())[0]
            if (kind == '电子发票' and len(re.findall(r'[\u4e00-\u9fff]', value)) < 2
                    and (len(re.findall(r'[\u4e00-\u9fff]', category)) < 2
                         or re.search(r'规格|单位|税率|税额|征收|数量|单价', category))):
                continue  # 乱码表头不能当品名；有正常税务分类的 USB3.0、U盘仍可读取。
            if value and value not in values:
                values.append(value)
    if not values and kind == '购买记录':
        # 仅用商品区域的结构线索。推荐商品区、订单信息和服务区不参与提取。
        active = True
        for index, line in enumerate(lines):
            if re.search(r'订单信息|服务保障|推荐商品|猜你喜欢|为你推荐|相似商品|你可能还喜欢', line):
                active = False
            if not active:
                continue
            priced = bool(re.search(r'[¥￥]|(?<![A-Za-z])Y(?=\d)', line))
            title = _title(line)
            # 相邻的商品规格会再次显示单价。括号、颜色/尺寸/发货说明是规格
            # 证据；不能把它们当成第二件商品，更不能只把规格用作物品名称。
            specification = (re.match(r'^[（(【\[]', title)
                             or re.search(r'颜色随机|实际发|(?:米白|杏白|哑光白)色|\d+(?:\.\d+)?\s*(?:cm|厘米|毫米)', title, re.I))
            if specification:
                continue
            if priced and _plausible_title(title):
                # 同行文字+价格是商品标题证据；规格行由 VARIANT 排除。
                if title not in values:
                    values.append(title)
                continue
            # 标题无同行价格时，要求紧邻价格/数量，并在遇到付款等字段前结束。
            if not _plausible_title(title) or priced:
                continue
            nearby = []
            for following in lines[index + 1:index + 4]:
                if UI_LINE.search(following):
                    break
                nearby.append(following)
            price = any(CURRENCY.search(part) for part in nearby)
            quantity = any(QUANTITY.search(part) for part in nearby)
            if price and quantity and title not in values:
                # 连续标题行属于同一商品卡片，拼接后只计一个候选。
                if values and index and _title(lines[index - 1]) == values[-1]:
                    values[-1] = (values[-1] + ' ' + title)[:120]
                else:
                    values.append(title)
    if len(values) == 1:
        return values[0], []
    if len(values) > 1:
        return '', ['包含多个商品或候选标题: ' + '、'.join(values)]
    return '', ['未找到明确的物品名称']


def extract_order_ids(text):
    """只读编号字段或订单信息区，不把电话、日期、银行卡号当作订单号。"""
    lines, values = text.splitlines(), set()
    label = re.compile(r'(?:订单编号|订单号码|订单编码|商家订单号|订单号|交易单号|商户单号)\s*[:：]?\s*')
    for index, line in enumerate(lines):
        for match in label.finditer(line):
            value = line[match.end():]
            if not value.strip() and index + 1 < len(lines):
                value = lines[index + 1]
            value = re.split(r'复制|查看|交易快照', value)[0].strip(' :："“”\'')
            # OCR 在同一串数字中插空格可合并，但不跨越一整行或拼接第二个字段。
            if re.fullmatch(r'[\d ]+', value):
                value = value.replace(' ', '')
            found = re.match(r'([A-Za-z0-9_][A-Za-z0-9_-]{5,63})(?![A-Za-z0-9_-])', value)
            if found and re.search(r'\d', found[1]) and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', found[1]):
                values.add(found[1])
        if '订单信息' in line:
            for following in lines[index + 1:index + 3]:
                found = re.fullmatch(r'(\d{10,64})\s*(?:复制.*)?', following)
                if found:
                    values.add(found[1])
    return sorted(values)
