"""多证据候选排序，与确定配对分开；分数只用于排序，不是正确率。

先排除编号冲突、已占用发票与不同金额，再综合名称、商家、日期排序。
相同金额且名称不同的候选可以出现，但永远需要用户明确确认。
"""
from copy import deepcopy
from datetime import date
from difflib import SequenceMatcher
from hashlib import sha256

from imss.core.matching import group_materials
from imss.core.models import normalized, STANDALONE_SHEETS


def proposal_id(invoice, order, members):
    identity = '\n'.join(sorted(m.source + ':' + m.digest for m in members))
    return sha256((invoice.source + '\n' + order.source + '\n' + identity).encode()).hexdigest()[:24]


def pair_evidence(invoice, order):
    reasons, risks, score = [], [], 0
    shared_id = False
    for field, label in (('order_ids', '订单号'), ('invoice_ids', '发票号')):
        left, right = set(getattr(invoice, field)), set(getattr(order, field))
        if left and right:
            if not left & right:
                return None  # 明确编号冲突不能由金额/名称抵消。
            shared_id = True
            reasons.append(label + '一致')
            score += 100
    if invoice.amount and order.amount:
        if invoice.amount != order.amount:
            return None
        reasons.append('金额一致 ' + invoice.amount)
        score += 40
    elif not shared_id:
        return None
    else:
        risks.append('一方金额缺失，将采用发票金额，请核实')
    if invoice.category != order.category and '物品' not in (invoice.category, order.category, ''):
        return None
    original_item = '' if order.match_basis == 'amount_only' else order.item
    left, right = normalized(invoice.item), normalized(original_item)
    if left and right:
        if left == right:
            reasons.append('物品名称一致')
            score += 35
        elif min(len(left), len(right)) >= 2 and (left in right or right in left):
            reasons.append('商品标题包含发票名称')
            score += 25
        else:
            similarity = SequenceMatcher(None, left, right).ratio()
            if similarity >= .45:
                reasons.append(f'名称部分相似（{similarity:.0%} 字符相似度）')
                score += round(similarity * 20)
        if left != right:
            risks.append('名称不同；确认后统一使用发票物品名称')
    else:
        risks.append('订单或发票名称缺失')
    if invoice.merchant and order.merchant and normalized(invoice.merchant) == normalized(order.merchant):
        reasons.append('商家一致')
        score += 20
    if invoice.transaction_date and order.transaction_date:
        delta = (date.fromisoformat(invoice.transaction_date) - date.fromisoformat(order.transaction_date)).days
        if 0 <= delta <= 30:
            reasons.append(f'开票在交易后 {delta} 天')
            score += 5
        else:
            risks.append('开票与交易日期相距较远或先后异常')
    if score == 40:
        risks.insert(0, '只有金额支持，不能证明同一笔交易')
    return score, reasons, risks


def pairing_candidates(materials):
    """为未完成订单列出全部兼容发票，标明排序与争用；不修改输入材料。"""
    records = deepcopy(materials)
    bundles, pending = group_materials([m for m in records if m.kind not in STANDALONE_SHEETS])
    anchors = []
    orders = {m.source: m for m in pending if m.kind in ('购买记录', '购买清单') and not m.group}
    for bundle in bundles:
        invoices = [m for m in bundle.materials if m.kind == '电子发票']
        purchases = [m for m in bundle.materials if m.kind in ('购买记录', '购买清单')]
        if len(invoices) != 1 or invoices[0].confirmed or invoices[0].group:
            continue
        # 只把已有的金额兜底建议重新列入确认，明确配对不被其他订单争抢。
        allowed_orders = None
        if purchases and not all(m.match_basis == 'amount_only' and not m.confirmed for m in purchases):
            if len(purchases) != 1 or not bundle.issues or purchases[0].confirmed:
                continue
            allowed_orders = {purchases[0].source}
        orders.update({m.source: m for m in purchases})
        anchors.append((invoices[0], [m for m in bundle.materials if m.kind not in ('购买记录', '购买清单')], allowed_orders))
    proposals = []
    for order in orders.values():
        if order.confirmed or order.group:
            continue
        for invoice, attachments, allowed_orders in anchors:
            if allowed_orders is not None and order.source not in allowed_orders:
                continue
            evidence = pair_evidence(invoice, order)
            if not evidence or not invoice.item or not invoice.amount:
                continue
            score, reasons, risks = evidence
            members = attachments + [order]
            risks += [f'{m.kind}：{issue}' for m in members for issue in m.issues
                      if not any(word in issue for word in ('没有唯一匹配', '多个候选', '仅按相同金额'))]
            if any(m.category == '行程' for m in members) and not any(m.kind == '行程单' for m in members):
                risks.append('行程报销缺少行程单；确认仍不能生成，请补齐或手工关联')
            proposals.append(dict(id=proposal_id(invoice, order, members), invoice=invoice.source,
                order=order.source, members=[m.source for m in members], score=score,
                reasons='；'.join(reasons), risks='；'.join(dict.fromkeys(risks)),
                item=invoice.item, order_item='' if order.match_basis == 'amount_only' else order.item, amount=invoice.amount,
                status='', rank=0, confirmed=''))
    for proposal in proposals:
        alternatives = sorted((p for p in proposals if p['order'] == proposal['order']), key=lambda p: (-p['score'], p['invoice']))
        rivals = [p for p in proposals if p['invoice'] == proposal['invoice'] and p['order'] != proposal['order']]
        proposal['rank'] = alternatives.index(proposal) + 1
        unique = len(alternatives) == 1 and not rivals
        proposal['status'] = '双方唯一候选' if unique else '存在竞争候选，请只选一组'
    return sorted(proposals, key=lambda p: (p['status'] != '双方唯一候选', p['order'], p['rank']))
