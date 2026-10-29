"""按证据强弱配对；金额兜底只提出建议，不能直接放行生成。"""
from collections import defaultdict
from decimal import Decimal
import re

from imss.core.models import Bundle, MATERIALS, Material, normalized


def group_materials(materials: list[Material]) -> tuple[list[Bundle], list[Material]]:
    """先完成明确匹配，再对双方唯一且未配对的同金额订单提出待核查建议。"""
    candidates = [m for m in materials if m.kind in MATERIALS and not m.duplicate_of]
    groups: dict[str, list[Material]] = defaultdict(list)
    pending: list[Material] = []
    deferred = []
    # Invoices are anchors. Multiple anchors are kept separate unless grouped manually.
    invoices = [m for m in candidates if m.kind == "电子发票"]
    for invoice in invoices:
        key = "manual:" + invoice.group if invoice.group else "invoice:" + invoice.digest
        groups[key].append(invoice)
    # Relational documents (itineraries/waybills) are attached first, so their
    # transaction IDs can then connect screenshots to the same invoice.
    candidates.sort(key=lambda m: (0 if m.kind in ("行程单", "运单明细") else 1, m.source))
    for material in candidates:
        if material.kind == "电子发票":
            continue
        if material.group:
            keys = [part.strip() for part in material.group.split('|') if part.strip()]
            if len(keys) > 1 and material.kind != "签领表":
                raise ValueError("只有签领表允许用 | 指定多个共享分组")
            for key in keys:
                groups["manual:" + key].append(material)
            continue
        matches = []
        for key, anchors in list(groups.items()):
            if key.startswith("manual:"):
                continue
            for anchor in anchors:
                if any(getattr(anchor, field) and getattr(material, field)
                       and not set(getattr(anchor, field)) & set(getattr(material, field))
                       for field in ('order_ids', 'invoice_ids')):
                    continue  # 其他相似证据不能掩盖明确编号冲突。
                shared_order = bool(set(anchor.order_ids) & set(material.order_ids))
                shared_invoice = bool(set(anchor.invoice_ids) & set(material.invoice_ids))
                different_order = bool(anchor.order_ids and material.order_ids and not shared_order)
                exact = (material.item and material.amount and normalized(material.item) == normalized(anchor.item)
                         and material.amount == anchor.amount and not different_order)
                # A unique travel total can connect an itinerary to its invoice;
                # goods with unrelated names still require a meaningful identifier.
                relational = (material.kind in ("行程单", "运单明细") and anchor.kind == "电子发票"
                              and material.category == anchor.category and material.amount
                              and material.amount == anchor.amount)
                itinerary_part = (material.kind == "购买记录" and material.category == "行程" and anchor.kind == "行程单"
                                  and material.amount and material.amount in anchor.related_amounts)
                if shared_order or shared_invoice or exact or relational or itinerary_part:
                    matches.append(key)
                    break
        if len(matches) == 1:
            anchor = next(m for m in groups[matches[0]] if m.kind == "电子发票")
            # A shared order ID is sufficient to fill an otherwise blank declaration field.
            # A conflicting nonempty value is retained, so bundle validation catches it.
            if (set(anchor.order_ids) & set(material.order_ids)) or (set(anchor.invoice_ids) & set(material.invoice_ids)) or material.kind in ("行程单", "运单明细"):
                if not material.item and anchor.item:
                    material.item = anchor.item
                    material.issues = [issue for issue in material.issues if issue != "未找到明确的物品名称"]
                if not material.amount and anchor.amount:
                    material.amount = anchor.amount
                    material.issues = [issue for issue in material.issues if issue != "未找到明确的含税合计或实付金额"]
            groups[matches[0]].append(material)
        else:
            deferred.append((material, matches))
    # 先收集所有候选，再检查两端唯一，避免文件排序决定金额建议配对的结果。
    proposals = {}
    for material, matches in deferred:
        if matches or material.kind != '购买记录' or material.item or not material.amount:
            continue
        possible = []
        for key, records in groups.items():
            if key.startswith('manual:') or any(m.kind in ('购买记录', '购买清单') for m in records):
                continue
            invoice = next(m for m in records if m.kind == '电子发票')
            conflicting_id = any(
                getattr(invoice, field) and getattr(material, field)
                and not set(getattr(invoice, field)) & set(getattr(material, field))
                for field in ('order_ids', 'invoice_ids'))
            if invoice.item and invoice.amount == material.amount and not conflicting_id:
                possible.append(key)
        proposals[material.source] = possible
    for material, matches in deferred:
        possible = proposals.get(material.source, [])
        unique = len(possible) == 1 and sum(possible[0] in keys for keys in proposals.values()) == 1
        if unique:
            key = possible[0]
            invoice = next(m for m in groups[key] if m.kind == '电子发票')
            material.item = invoice.item
            material.match_basis = 'amount_only'
            material.issues = [issue for issue in material.issues if issue != '未找到明确的物品名称']
            material.issues.append('仅按相同金额匹配唯一未配对发票，物品名称来自发票；可能配错，请核对原件后确认')
            groups[key].append(material)
        else:
            issue = ('匹配到多个候选或多份订单争用同金额发票，请填写分组'
                     if matches or possible else '没有唯一匹配的发票，请填写分组')
            if issue not in material.issues:
                material.issues.append(issue)
            pending.append(material)
    bundles = []
    def group_order(pair):
        key, records = pair
        if key.startswith("manual:"):
            # Explicit group IDs provide a reproducible order even when source
            # filenames are random: 2, 10, 24.1, 24.2, 25.
            parts = re.split(r"(\d+)", key.removeprefix("manual:"))
            return (0, tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts))
        return (1, min(m.source for m in records))

    for key, records in sorted(groups.items(), key=group_order):
        items = {normalized(m.item): m.item for m in records if m.item and m.kind not in ("签领表", "购买清单")}
        amounts = {m.amount for m in records if m.amount and m.kind == "电子发票"}
        issues = []
        if len(items) != 1:
            issues.append("组内物品名称缺失或不一致，请统一核对清单中的物品名称")
        if len(amounts) != 1:
            issues.append("组内金额缺失或不一致，请核对清单中的金额")
        total = next(iter(amounts), "")
        proof_amounts = [m.amount for m in records if m.amount and m.kind in ("购买记录", "购买清单")]
        if total and proof_amounts and not all(amount == total for amount in proof_amounts):
            grounded = any(m.kind == '行程单' and m.amount == total and all(amount in m.related_amounts for amount in proof_amounts) for m in records)
            if (not key.startswith("manual:") and not grounded) or sum(Decimal(amount) for amount in proof_amounts) != Decimal(total):
                issues.append("购买记录金额与发票不一致，或分笔金额之和不等于发票总额")
        if total and any(m.amount and m.amount != total for m in records if m.kind in ("行程单", "运单明细", "承诺书")):
            issues.append("行程单、运单或承诺书总额与发票不一致")
        if any(m.issues or (m.kind not in ("签领表", "承诺书", "购买清单") and (not m.item or not m.amount)) for m in records):
            issues.append("组内材料存在待核对字段或提取错误")
        if sum(m.kind == "电子发票" for m in records) != 1:
            issues.append("每组需要且只能有一张发票；多张发票请拆分 group")
        if not any(m.kind in ("购买记录", "购买清单") for m in records):
            issues.append("缺少购买记录或购买清单")
        if any(m.category == "行程" for m in records) and not any(m.kind == "行程单" for m in records):
            issues.append("行程报销缺少行程单")
        bundles.append(Bundle(key, next(iter(items.values()), ""), next(iter(amounts), ""), records, issues))
    return bundles, pending
