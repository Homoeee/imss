"""Extract templates, retaining the explicitly requested promise image only."""
import argparse
from copy import deepcopy
from pathlib import Path
import re
import sys

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_ROW_HEIGHT_RULE, WD_CELL_VERTICAL_ALIGNMENT

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from imss.output.documents import replace_fields


def set_cell(cell, text):
    # Retain the first paragraph's properties and the original grid/merge geometry.
    paragraphs = cell.paragraphs
    paragraph = paragraphs[0]
    run_properties = deepcopy(paragraph.runs[0]._r.rPr) if paragraph.runs and paragraph.runs[0]._r.rPr is not None else None
    for child in list(paragraph._element):
        if child.tag != qn("w:pPr"):
            paragraph._element.remove(child)
    run = paragraph.add_run(text)
    if run_properties is not None:
        run._r.insert(0, run_properties)
    run.font.size = Pt(8)
    for extra in paragraphs[1:]:
        extra._element.getparent().remove(extra._element)


def distill(source: Path, destination: Path, travel=False, promise_image: Path | None = None):
    reference = Document(source)
    document = Document()
    body = document.element.body
    for child in list(body):
        body.remove(child)
    document.part._styles_part._element = deepcopy(reference.styles.element)
    section = deepcopy(reference.sections[0]._sectPr)
    for element in list(section):
        if element.tag in (qn("w:headerReference"), qn("w:footerReference")):
            section.remove(element)
    body.append(deepcopy(reference.tables[0]._element))
    for marker in ("invoice_panel", "evidence_panel"):
        paragraph = OxmlElement("w:p")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "{{" + marker + "}}"
        run.append(text)
        paragraph.append(run)
        body.append(paragraph)
    body.append(deepcopy(reference.tables[-1]._element))
    body.append(OxmlElement("w:p"))
    body.append(section)
    # Remove hidden/fallback image nodes from the cloned table structures too.
    for element in list(body.xpath('.//w:drawing | .//w:pict')):
        element.getparent().remove(element)
    header, declaration = document.tables
    if travel:
        set_cell(header.cell(0, 0), "发票类别：打车发票")
        set_cell(header.cell(0, 1), "发票编号：{{invoice_number}}")
        for index in range(3):
            set_cell(declaration.cell(index, 0), f"乘车人{index + 1}签名：" + ("{{signature_image}}" if index == 0 else ""))
            set_cell(declaration.cell(index, 3), "学号：{{student_id}}" if index == 0 else "学号：")
        set_cell(declaration.cell(3, 0), "乘车事由：")
        set_cell(declaration.cell(3, 1), "{{purpose}}")
        set_cell(declaration.cell(4, 0), "请开票人在右侧签“承诺不重复报销”：")
        set_cell(declaration.cell(4, 2), "")
        promise_cell = declaration.cell(4, 2)
        promise_row = declaration.rows[4]
    else:
        set_cell(header.cell(0, 0), "发票类别：网购发票")
        set_cell(header.cell(0, 1), "物品名称（用途）：{{purpose}}")
        set_cell(header.cell(0, 2), "发票编号：{{invoice_number}}")
        set_cell(declaration.cell(0, 0), "购买人（开票人）签名：{{signature_image}}")
        set_cell(declaration.cell(0, 2), "学号：{{student_id}}")
        set_cell(declaration.cell(0, 6), "联系方式：{{phone}}")
        set_cell(declaration.cell(1, 0), "请在右侧签“承诺不重复报销”：")
        set_cell(declaration.cell(1, 3), "")
        promise_cell = declaration.cell(1, 3)
        promise_row = declaration.rows[1]
        set_cell(declaration.cell(2, 0), "此发票是否为奖品发票：")
        set_cell(declaration.cell(2, 1), "{{prize_no}}否")
        set_cell(declaration.cell(2, 5), "{{prize_yes}}是，签领单附后")
        set_cell(declaration.cell(3, 0), "发票金额与实际报销金额是否一致：")
        set_cell(declaration.cell(3, 2), "{{equal_yes}}是")
        set_cell(declaration.cell(3, 4), "{{equal_no}}否，实报金额应为 {{actual_amount}} 元，原因如下（手写）：")
    if promise_image:
        paragraph = promise_cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1
        promise_row.height = Mm(10)
        promise_row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
        promise_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        picture = paragraph.add_run()
        picture.font.size = Pt(1)
        picture.add_picture(str(promise_image), width=Mm(33))
    # Only the authorized standalone promise image is retained. Buyer names,
    # personal IDs, phone numbers and their signature images are removed.
    for tag in ('w:commentRangeStart', 'w:commentRangeEnd', 'w:commentReference', 'w:del', 'w:ins'):
        for element in list(body.xpath('.//' + tag)):
            element.getparent().remove(element)
    properties = document.core_properties
    properties.author = properties.last_modified_by = ""
    properties.title = "报销材料通用模板"
    destination.parent.mkdir(parents=True, exist_ok=True)
    document.save(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reference', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--promise-image', type=Path, help='显式提供承诺文字原图；默认提取 01 参考单的独立承诺图')
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    promise_image = args.promise_image
    if promise_image is None:
        reference = Document(next(args.reference.glob('01_*.docx')))
        cell = reference.tables[-1].cell(1, 3)
        relationship = cell._tc.xpath('.//a:blip/@r:embed')[0]
        part = reference.part.related_parts[relationship]
        promise_image = args.destination / ('承诺不重复报销' + Path(str(part.partname)).suffix)
        promise_image.write_bytes(part.blob)
    # Layout and fields come from the two observed form families. Subtypes keep
    # distinct template paths so they can evolve independently as rules grow.
    selectors = {'物品': '01_', '奖品': '18.', '工作餐': '17.', '行程': '20.', '快递': '23.'}
    for category, prefix in selectors.items():
        source = next(args.reference.glob(prefix + '*.docx'))
        destination = args.destination / (category + '.docx')
        distill(source, destination, category == '行程', promise_image=promise_image)
        print(category, destination)
