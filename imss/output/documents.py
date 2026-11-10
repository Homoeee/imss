"""Template fields stay editable; supporting materials are inserted as full-page images."""
import io
from contextlib import ExitStack
from itertools import zip_longest
from pathlib import Path
import re
from copy import deepcopy
from PIL import Image

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Mm, Pt, RGBColor

from imss.recognition.extract import material_pages
from imss.core.models import Bundle, MATERIALS, STANDALONE_SHEETS, Settings

MARKERS = {"invoice_images": "电子发票", "purchase_images": "购买记录", "promise_images": "承诺书", "all_materials": "all"}
MEAL_PANEL = "{{meal_panel}}"
PANEL_MARKERS = ("{{invoice_panel}}", "{{evidence_panel}}", MEAL_PANEL)


def validate_signature(path: Path):
    if not path.is_file():
        raise ValueError(f"签字图片不存在: {path}")
    with Image.open(path) as image:
        if image.format not in ('PNG', 'JPEG', 'BMP', 'GIF', 'TIFF'):
            raise ValueError("签字图片请使用 PNG、JPG、BMP、GIF 或 TIFF")
        image.verify()


def replace_signature(paragraph, path: Path | None, field='signature_image'):
    if not re.search(r'{{\s*' + re.escape(field) + r'\s*}}', paragraph.text):
        return
    marker = '\ufffc'
    replace_fields(paragraph, {field: marker if path else ''})
    if not path:
        return
    with Image.open(path) as image:
        width, height = image.size
    factor = min(23 / width, 7 / height)
    for run in list(paragraph.runs):
        if marker not in run.text:
            continue
        parts = run.text.split(marker)
        run.text = parts[0]
        previous = run
        for following in parts[1:]:
            picture = paragraph.add_run()
            picture.font.size = Pt(1)
            previous._r.addnext(picture._r)
            picture.add_picture(str(path), width=Mm(width * factor), height=Mm(height * factor))
            tail = paragraph.add_run(following)
            if run._r.rPr is not None:
                tail._r.insert(0, deepcopy(run._r.rPr))
            picture._r.addnext(tail._r)
            previous = tail


def append_placeholder(paragraph, field):
    """新增占位符沿用标签字号，防止同行人的学号被默认样式放大。"""
    previous = paragraph.runs[-1] if paragraph.runs else None
    run = paragraph.add_run('{{' + field + '}}')
    if previous is not None and previous._r.rPr is not None:
        run._r.insert(0, deepcopy(previous._r.rPr))


def paragraphs_in(container):
    for paragraph in container.paragraphs:
        yield paragraph
    for table in container.tables:
        seen = set()
        for row in table.rows:
            for cell in row.cells:
                if cell._tc in seen:
                    continue
                seen.add(cell._tc)
                yield from paragraphs_in(cell)


def replace_fields(paragraph, fields):
    # Replace across split Word runs without erasing the paragraph or its drawings.
    pattern = re.compile(r"{{\s*(" + "|".join(re.escape(key) for key in fields) + r")\s*}}")
    for match in reversed(list(pattern.finditer(paragraph.text))):
        position = 0
        started = False
        for run in paragraph.runs:
            text = run.text
            start, end = position, position + len(text)
            position = end
            if end <= match.start() or start >= match.end():
                continue
            left, right = max(0, match.start() - start), min(len(text), match.end() - start)
            replacement = str(fields[match.group(1)]) if not started else ""
            run.text = text[:left] + replacement + text[right:]
            started = True


def create_template(path: Path):
    if path.exists():
        raise FileExistsError(f"拒绝覆盖模板: {path}")
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.top_margin = section.bottom_margin = Mm(18)
    section.left_margin = section.right_margin = Mm(18)
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2"):
        style = document.styles[style_name]
        style.font.name = "宋体"
        style.font.color.rgb = RGBColor(0, 0, 0)
        from docx.oxml.ns import qn
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    document.styles["Normal"].font.size = Pt(11)
    document.add_paragraph("报销材料汇编", "Title")
    document.add_paragraph("序号：{{sequence}}    物品：{{item_name}}    金额：{{amount}} 元")
    document.add_paragraph("本文件汇集该笔支出的发票、购买记录及承诺书（如有），供报销核对。")
    document.add_paragraph("{{invoice_images}}")
    document.add_paragraph("{{purchase_images}}")
    document.add_paragraph("{{promise_images}}")
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)


def prepare_document(bundle: Bundle, sequence: str, template: Path | None, details=None):
    """Fill the form fields and signatures shared by Word and PDF output."""
    details = details or {}
    signatures = {}
    for field in ('signature_image', 'signature_image_2', 'signature_image_3'):
        signatures[field] = Path(details[field]) if details.get(field) else None
        if signatures[field]:
            validate_signature(signatures[field])
    builtin = not template
    if not template:
        category = details.get("category") or next((m.category for m in bundle.materials if m.kind == "电子发票"), "物品") or "物品"
        derived = Path(__file__).resolve().parents[1] / "resources" / "templates" / (category + ".docx")
        if derived.is_file():
            template = derived
    document = Document(template) if template else Document()
    if builtin and category == '行程' and document.tables:
        # 内置表保留三行布局，为同行人的原空栏补占位符。
        declaration = document.tables[-1]
        for index in (1, 2):
            for paragraph in declaration.cell(index, 0).paragraphs:
                if '签名：' in paragraph.text and '{{signature_image' not in paragraph.text:
                    append_placeholder(paragraph, 'signature_image_' + str(index + 1))
            for paragraph in declaration.cell(index, 3).paragraphs:
                if '学号：' in paragraph.text and '{{student_id' not in paragraph.text:
                    append_placeholder(paragraph, 'student_id_' + str(index + 1))
    if not template:
        section = document.sections[0]
        section.page_width, section.page_height = Mm(210), Mm(297)
        section.left_margin = section.right_margin = Mm(18)
        section.top_margin = section.bottom_margin = Mm(18)
        document.add_paragraph(f"{sequence} {bundle.item}", "Title")
        document.add_paragraph(f"报销金额：{bundle.amount} 元")
        document.add_paragraph("{{all_materials}}")
    invoice = next((m for m in bundle.materials if m.kind == "电子发票"), None)
    prize = details.get("is_prize", 0) == 1
    fields = {"sequence": sequence, "item_name": bundle.item, "amount": bundle.amount,
              "invoice_number": details.get("invoice_number", invoice.invoice_ids[0] if invoice and invoice.invoice_ids else ""),
              "purpose": details.get("purpose", bundle.item), "student_id": details.get("student_id", ""),
              "passenger_name": details.get("passenger_name", ""),
              "passenger_name_2": details.get("passenger_name_2", ""),
              "passenger_name_3": details.get("passenger_name_3", ""),
              "phone": details.get("phone", ""), "actual_amount": details.get("actual_amount", ""),
              "prize_yes": "☑" if prize else "□", "prize_no": "□" if prize else "☑",
              "equal_yes": "☑", "equal_no": "□"}
    fields.update({key: details.get(key, '') for key in ('student_id_2', 'student_id_3')})
    containers = [document]
    for section in document.sections:
        # Accessing paragraphs on an undefined header or footer creates a blank
        # part that can take up page space. Inherited parts are filled at their defining section.
        containers.extend(part for part in (section.header, section.footer, section.first_page_header,
                                            section.first_page_footer, section.even_page_header,
                                            section.even_page_footer) if not part.is_linked_to_previous)
    for container in containers:
        for paragraph in paragraphs_in(container):
            replace_fields(paragraph, fields)
            for field, signature in signatures.items():
                replace_signature(paragraph, signature, field)
    return document


def build_document(bundle: Bundle, sequence: str, template: Path | None, destination: Path, settings: Settings, details=None):
    """按类别填充可编辑表格并插入附件，签领表单独输出。"""
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖: {destination}")
    document = prepare_document(bundle, sequence, template, details)
    materials = [m for m in bundle.materials if m.kind not in STANDALONE_SHEETS]
    panels = [(p, p.text.strip()) for p in paragraphs_in(document) if p.text.strip() in PANEL_MARKERS]
    if panels:
        if len(panels) != 2 or {text for _, text in panels} not in (
                {"{{invoice_panel}}", "{{evidence_panel}}"}, {"{{invoice_panel}}", MEAL_PANEL}):
            raise ValueError("单页模板需要一个发票区和一个附件区")
        for marker, panel in panels:
            selected = [m for m in materials if (m.kind == "电子发票") == (panel == "{{invoice_panel}}")]
            if panel == MEAL_PANEL:
                selected.extend(m for m in bundle.materials if m.kind == '签领表')
            insert_panel(document, marker, selected, settings, meal=panel == MEAL_PANEL)
        destination.parent.mkdir(parents=True, exist_ok=True)
        document.save(destination)
        return
    markers = []
    for paragraph in paragraphs_in(document):
        match = re.fullmatch(r"\s*{{\s*(\w+)\s*}}\s*", paragraph.text)
        if match and match.group(1) in MARKERS:
            markers.append((paragraph, MARKERS[match.group(1)]))
        elif any(re.search(r"{{\s*" + key + r"\s*}}", paragraph.text) for key in MARKERS):
            raise ValueError("图片占位符必须独占一段，不能与文字混排")
    marker_kinds = [kind for _, kind in markers]
    if len(marker_kinds) != len(set(marker_kinds)) or ("all" in marker_kinds and len(marker_kinds) > 1):
        raise ValueError("模板图片占位符重复，或 all_materials 与分类占位符混用")
    # Existing templates with no image markers still receive all attachments at the end.
    if not markers:
        markers = [(document.add_paragraph(), "all")]
    used = set()
    for marker, kind in markers:
        selected = [m for m in materials if kind == "all" or m.kind == kind]
        selected.sort(key=lambda m: (MATERIALS.index(m.kind), m.source))
        for material in selected:
            insert_material(document, marker, material, settings)
            used.add(material.source)
        marker._element.getparent().remove(marker._element)
    # A template may contain only one of the category markers. Never drop other materials.
    for material in sorted(materials, key=lambda m: (MATERIALS.index(m.kind), m.source)):
        if material.source not in used:
            marker = document.add_paragraph()
            insert_material(document, marker, material, settings)
            marker._element.getparent().remove(marker._element)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".partial.docx")
    try:
        document.save(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def insert_material(document, marker, material, settings):
    # Use the smallest printable area in the template so mixed sections cannot overflow.
    width = min(s.page_width - s.left_margin - s.right_margin for s in document.sections)
    height = min(s.page_height - s.top_margin - s.bottom_margin for s in document.sections) - Mm(20)
    cell = marker._element.getparent()
    if cell.tag.endswith("}tc"):
        from docx.oxml.ns import qn
        cell_width = cell.find("./" + qn("w:tcPr") + "/" + qn("w:tcW"))
        if cell_width is not None and cell_width.get(qn("w:type")) == "dxa":
            width = min(width, int(cell_width.get(qn("w:w"))) * 635 - Mm(4))
    if width <= 0 or height <= 0:
        raise ValueError("模板可用页面尺寸过小")
    count = 0
    for index, image in enumerate(material_pages(Path(material.archived), material.extension, settings)):
        try:
            caption = document.add_paragraph()
            caption.paragraph_format.page_break_before = True
            caption.paragraph_format.keep_with_next = True
            caption.paragraph_format.space_after = Pt(4)
            caption.add_run(f"{material.kind}  第 {index + 1} 页").bold = True
            marker._element.addprevious(caption._element)
            paragraph = document.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1
            factor = min(width / image.width, height / image.height)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            buffer.seek(0)
            paragraph.add_run().add_picture(buffer, width=int(image.width * factor), height=int(image.height * factor))
            marker._element.addprevious(paragraph._element)
            count += 1
        finally:
            image.close()
    if not count:
        raise ValueError(f"材料没有可插入页面: {material.source}")


def panel_images(materials, settings, stack):
    images = []
    for material in sorted(materials, key=lambda m: (MATERIALS.index(m.kind), m.source)):
        for image in material_pages(Path(material.archived), material.extension, settings):
            stack.callback(image.close)
            images.append(image)
    return images


def meal_rows(materials, settings, stack):
    evidence = panel_images([m for m in materials if m.kind != '签领表'], settings, stack)
    sheets = panel_images([m for m in materials if m.kind == '签领表'], settings, stack)
    return list(zip_longest(evidence, sheets)) or [(None, None)]


def insert_panel(document, marker, materials, settings, *, meal=False):
    """Insert the evidence images, reserving a second column for meal signatures."""
    with ExitStack() as stack:
        if meal:
            rows = meal_rows(materials, settings, stack)
        else:
            pages = panel_images(materials, settings, stack)
            if not pages:
                marker._element.getparent().remove(marker._element)
                return
            if len(pages) > 6:
                raise ValueError("单页模板每个图片区最多容纳 6 页；请拆分该组或使用多页模板")
            rows = [pages]
        section = document.sections[0]
        width = section.page_width - section.left_margin - section.right_margin
        cols = len(rows[0])
        column_width = int(width / cols)
        table = document.add_table(rows=len(rows), cols=cols)
        table.autofit = False
        from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        properties = table._tbl.tblPr
        borders = OxmlElement('w:tblBorders')
        for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
            border = OxmlElement('w:' + edge)
            border.set(qn('w:val'), 'dashed')
            border.set(qn('w:sz'), '4')
            border.set(qn('w:color'), '000000')
            borders.append(border)
        properties.insert_element_before(borders, 'w:shd', 'w:tblLayout', 'w:tblCellMar', 'w:tblLook')
        margins = OxmlElement('w:tblCellMar')
        for edge in ('top', 'bottom'):
            margin = OxmlElement('w:' + edge)
            margin.set(qn('w:w'), '40')
            margin.set(qn('w:type'), 'dxa')
            margins.append(margin)
        properties.insert_element_before(margins, 'w:tblLook')
        for column in table.columns:
            column.width = column_width
        for row, entries in zip(table.rows, rows):
            for cell, entry in zip(row.cells, entries):
                cell.width = column_width
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                paragraph = cell.paragraphs[0]
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.space_before = paragraph.paragraph_format.space_after = Pt(0)
                paragraph.paragraph_format.line_spacing = 1
                paragraph.paragraph_format.keep_with_next = False
                if entry is not None:
                    factor = min((column_width - Mm(4)) / entry.width, Mm(108) / entry.height)
                    stream = io.BytesIO()
                    entry.save(stream, format='PNG')
                    stream.seek(0)
                    paragraph.add_run().add_picture(stream, width=int(entry.width * factor), height=int(entry.height * factor))
        # Word merges adjacent tables without an intervening paragraph; that can
        # erase neighboring form borders and make pictures touch the declaration.
        for element in ("before", "table", "after"):
            if element == "table":
                marker._element.addprevious(table._tbl)
            else:
                spacer = document.add_paragraph()
                spacer.paragraph_format.space_before = spacer.paragraph_format.space_after = Pt(0)
                spacer.paragraph_format.line_spacing = Pt(2)
                spacer.add_run().font.size = Pt(1)
                marker._element.addprevious(spacer._element)
        marker._element.getparent().remove(marker._element)
