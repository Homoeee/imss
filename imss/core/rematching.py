"""已校验材料重配对：不继承旧人工分组，唯一同名发票支持分笔合计。"""
from collections import defaultdict
from decimal import Decimal

from imss.core.models import normalized
from imss.core.matching import group_materials


def group_verified_materials(materials):
    for material in materials:
        material.group = ''
    invoices = defaultdict(list)
    for material in materials:
        if material.kind == '电子发票' and not material.duplicate_of:
            invoices[normalized(material.item)].append(material)
    for name, anchors in invoices.items():
        if not name or len(anchors) != 1 or not anchors[0].amount:
            continue
        invoice = anchors[0]
        proofs = [m for m in materials if m.kind in ('购买记录', '购买清单') and not m.duplicate_of
                  and normalized(m.item) == name and m.amount]
        if len(proofs) < 2 or all(m.amount == invoice.amount for m in proofs):
            continue
        if any(getattr(invoice, field) and getattr(m, field)
               and not set(getattr(invoice, field)) & set(getattr(m, field))
               for m in proofs for field in ('order_ids', 'invoice_ids')):
            continue
        if sum(Decimal(m.amount) for m in proofs) != Decimal(invoice.amount):
            continue
        # 只有唯一同名锚点、整组总额相符且没有明确编号冲突，才建立内部组。
        # 此组不会保存为用户分组；下一次 build 重新从校验标志和覆盖值计算。
        key = 'reviewed-' + invoice.digest[:16]
        invoice.group = key
        for proof in proofs:
            proof.group = key
        for attachment in materials:
            if (attachment.kind in ('行程单', '运单明细', '承诺书')
                    and normalized(attachment.item) == name and attachment.amount == invoice.amount):
                attachment.group = key
    bundles, pending = group_materials(materials)
    for proof in pending:
        anchors = invoices.get(normalized(proof.item), [])
        if (proof.kind in ('购买记录', '购买清单') and len(anchors) == 1
                and proof.amount and anchors[0].amount and proof.amount != anchors[0].amount):
            issue = '购买记录金额与同名发票不一致，请核对金额或分笔支付总额'
            proof.issues.append(issue)
            for bundle in bundles:
                if anchors[0] in bundle.materials:
                    bundle.issues.append(issue)
    return bundles, pending
