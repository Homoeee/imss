"""旧工作簿测试夹具，不进入生产写出路径。"""
import csv
from decimal import Decimal, InvalidOperation
import math
import unicodedata
from pathlib import Path
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from imss.core.models import KINDS
from imss.core.candidates import pairing_candidates
from imss.output.candidate_review import write_candidates
from imss.output.review import review_rows
from imss.output.legacy_review import CSV_FIELDS, COLUMNS, OPTIONAL_COLUMNS
COLORS = {'严重': 'FADBD8', '待确认': 'FCE4C5', '提示': 'FFF2CC', '正常': 'E2F0D9'}
WIDTHS = (12, 65, 10, 26, 14, 16, 26, 44, 44, 32, 28, 18, 65)

def write_review_rows(path, rows, root, proposals=None):
    """共享渲染器：初次扫描和核对后刷新使用同样的列、配色和交互。"""
    if path.suffix.lower() == '.csv':
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        return
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '核对清单'
    sheet.append(['核对清单：红色=严重问题；橙色=金额建议配对；黄色=识别提示；绿色=通过配对校验。请先查看“候选配对”工作表，可批量确认。'])
    sheet.append(['点击原文件核查；修改名称、金额、类型、分组后，将已核对填 1。同组的所有相关行一起确认；不要删除行或改原文件列。'])
    sheet.append(['保存并关闭 Excel 后执行：python main.py build "' + str(path.with_suffix('.json').resolve()) + '"；将自动读取本表，重新验证后生成报销材料。'])
    for number in range(1, 4):
        sheet.merge_cells(start_row=number, start_column=1, end_row=number, end_column=len(COLUMNS + OPTIONAL_COLUMNS))
        sheet.cell(number, 1).alignment = Alignment(wrap_text=True, vertical='center')
        sheet.row_dimensions[number].height = 32
    sheet.append([label for _, label in COLUMNS + OPTIONAL_COLUMNS])
    for cell in sheet[4]:
        cell.fill = PatternFill('solid', fgColor='24445C')
        cell.font = Font(color='FFFFFF', bold=True, size=11)
    for row_number, row in enumerate(rows, 5):
        for col_number, (key, _) in enumerate(COLUMNS + OPTIONAL_COLUMNS, 1):
            cell = sheet.cell(row_number, col_number, row.get(key, ''))
            # OCR 文字与用户名称永远写作文本，防止 = 开头内容成为公式。
            cell.data_type = 's'
            cell.number_format = '@'
            if key == 'amount' and row.get(key):
                try:
                    amount = Decimal(row[key])
                    if amount.is_finite():
                        cell.value = amount
                        cell.number_format = '0.00'
                except InvalidOperation:
                    pass  # 未确认的手工输入原样保留，不能在刷新时偷偷改值。
            cell.fill = PatternFill('solid', fgColor=COLORS[row['severity']])
            cell.alignment = Alignment(vertical='top', wrap_text=True)
            cell.font = Font(name='微软雅黑', size=11, color='1261A0' if key in ('item', 'amount', 'kind', 'group', 'title', 'confirmed') else '202B33', bold=key in ('severity', 'issues') and row['severity'] != '正常')
            if key == 'source' and root:
                cell.hyperlink = (root / row['source']).resolve().as_uri()
                cell.font = Font(color='1261A0', underline='single')
            if key == 'group':
                cell.comment = Comment('同一笔支出的发票和订单填相同分组。改配对时两边一起改，并将已核对填1。分组决定配对和自然排序，最终文件使用连续整数编号。', 'IMSS')
            if key == 'issues' and row['issues']:
                cell.comment = Comment(row['issues'], 'IMSS')
        # 中文通常占两个英文字符宽度；随问题和路径长度增高，避免只显示前几条问题。
        line_counts = []
        for (key, _), width in zip(COLUMNS, WIDTHS):
            display_width = sum(2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
                                for char in str(row.get(key, '')))
            line_counts.append(max(1, math.ceil(display_width / max(1, width - 2))))
        sheet.row_dimensions[row_number].height = min(360, max(64 if row['issues'] else 38, max(line_counts) * 16 + 8))
    for col, width in zip('ABCDEFGHIJKLM', WIDTHS):
        sheet.column_dimensions[col].width = width
    for formula, column in [('"1,是"', 'C'), ('"' + ','.join(KINDS) + '"', 'F')]:
        validation = DataValidation(type='list', formula1=formula, allow_blank=True)
        validation.errorTitle = '请使用列表中的值'
        validation.error = '核对后填1或是；材料类型从下拉列表选择。'
        validation.showErrorMessage = True
        sheet.add_data_validation(validation)
        validation.add(f'{column}5:{column}{max(5, sheet.max_row)}')
    sheet.freeze_panes = 'D5'
    sheet.auto_filter.ref = f'A4:M{max(4, sheet.max_row)}'
    sheet.sheet_view.showGridLines = False
    write_candidates(workbook, proposals or [], root)
    temporary = path.with_suffix('.partial.xlsx')
    try:
        workbook.save(temporary)
        workbook.close()
        temporary.replace(path)
    except PermissionError as exc:
        raise PermissionError(f'核对清单无法写入，请保存并关闭 Excel 后重试：{path}') from exc
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)


def write_review(path, materials, root=None):
    rows = review_rows(materials)
    for row in rows:
        row["confirmed"] = "1" if next(m for m in materials if m.source == row["source"]).confirmed else ""
    write_review_rows(path, rows, root, pairing_candidates(materials))
