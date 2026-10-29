"""材料核对诊断；展示基于副本，八列 Excel 与旧表兼容各自独立。"""
from copy import deepcopy
from imss.core.models import STANDALONE_SHEETS
from imss.core.matching import group_materials

def review_rows(materials):
    """组内任意一份有问题，发票和订单一起前置，方便对照后统一修改。"""
    records = deepcopy(materials)
    bundles, _ = group_materials([m for m in records if m.kind not in STANDALONE_SHEETS])
    context = {}
    for bundle in bundles:
        invoices = [m for m in bundle.materials if m.kind == '电子发票']
        group = bundle.key.removeprefix('manual:') if bundle.key.startswith('manual:') else 'auto-' + invoices[0].digest[:16]
        row_issues = list(dict.fromkeys([issue for m in bundle.materials for issue in m.issues] + bundle.issues))
        for m in bundle.materials:
            context[m.source] = (group, row_issues, '；'.join(x.source for x in bundle.materials if x.source != m.source))
    rows = []
    for m in records:
        group, issues, matched = context.get(m.source, (m.group, list(m.issues), ''))
        if m.kind == '未知' and not issues:
            issues = ['未识别材料类型，请核对原件']
        if m.duplicate_of:
            issues = list(dict.fromkeys([*issues, '重复原件，仅组装一次：' + m.duplicate_of]))
        # 缺件、冲突和多候选优先级最高；金额建议配对不能显示为正常。
        serious = any(any(word in issue for word in ('缺少', '不一致', '冲突', '多个候选', '多个商品', '没有唯一', '失败', '损坏', '未知', '未识别', '只能有一张', '未找到明确', '金额不明确', '无法完整读取', '尚未付款')) for issue in issues)
        severity = '严重' if serious else '待确认' if any('仅按相同金额' in issue for issue in issues) else '提示' if issues else '正常'
        rows.append(dict(source=m.source, kind=m.kind, item=m.item, amount=m.amount,
                         group=m.group or group, title=m.title, confirmed='1' if m.confirmed else '',
                         issues='；'.join(issues), severity=severity, matched=matched,
                         merchant=m.merchant, transaction_date=m.transaction_date,
                         evidence=evidence_summary(m)))
    priority = {'严重': 0, '待确认': 1, '提示': 2, '正常': 3}
    return sorted(rows, key=lambda row: (priority[row['severity']], row['group'], row['source']))


def evidence_summary(material):
    output = []
    for field, entry in material.field_evidence.items():
        locations = [f"第{region['page']}页 [{', '.join(f'{n:.3f}' for n in region['box'])}]"
                     for region in entry.get('regions', [])]
        output.append(f"{field}={entry['value']}：{entry['method']} " + '；'.join(locations))
    return '\n'.join(output)


from imss.output.review_table import (COLUMNS, COLORS, WIDTHS, write_review, write_review_rows,
    refresh_review, read_review, apply_review, is_simple_review, migrated_states, view_rows)
