"""Render built-in reimbursement forms directly as PDF."""

from base64 import b64encode
from contextlib import ExitStack
from functools import cache
from io import BytesIO
from itertools import groupby
from pathlib import Path
from xml.sax.saxutils import escape

from docx.oxml.ns import qn
from docx.shared import Emu
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, Paragraph, Table

from imss.core.models import Bundle, STANDALONE_SHEETS, Settings
from imss.output.documents import MEAL_PANEL, meal_rows, panel_images, prepare_document

_FONT_NAME = 'IMSS Sans SC'
_MARGIN_X = 12 * mm
_MARGIN_Y = 7 * mm
_GAP = 3 * mm
_BODY_WIDTH = A4[0] - 2 * _MARGIN_X
_BODY_HEIGHT = A4[1] - 2 * _MARGIN_Y
_FRAME_PADDING = 2


@cache
def _form_style():
    font = Path(__file__).resolve().parents[1] / 'resources' / 'fonts' / 'IMSSSansSC-Regular.ttf'
    pdfmetrics.registerFont(TTFont(_FONT_NAME, str(font)))
    return ParagraphStyle('form', fontName=_FONT_NAME, fontSize=9, leading=12, wordWrap='CJK')


def _cell_content(cell, width):
    content = []
    style = _form_style()
    for paragraph in cell.paragraphs:
        text = paragraph.text.strip()
        if text.startswith('发票编号：'):
            text = text.replace('发票编号：', '发票编号：\n', 1)
        markup = escape(text).replace('\n', '<br/>')
        pictures = []
        inline_signature = False
        for drawing in paragraph._p.xpath('.//wp:inline'):
            extent = drawing.find(qn('wp:extent'))
            blip = drawing.find('.//' + qn('a:blip'))
            image = paragraph.part.related_parts[blip.get(qn('r:embed'))]
            w, h = (Emu(int(extent.get(axis))).pt for axis in ('cx', 'cy'))
            scale = min(1, (width - 8) / w)
            if text.endswith('签名：'):
                text_width = pdfmetrics.stringWidth(' '.join(text.split()), style.fontName, style.fontSize)
                remaining = width - 8 - text_width - 4
                if .75 * w * scale <= remaining < w * scale:
                    scale = remaining / w
                data = b64encode(image.blob).decode('ascii')
                markup += (f' <img src="data:{image.content_type};base64,{data}" '
                           f'width="{w * scale:.2f}" height="{h * scale:.2f}" valign="middle"/>')
                inline_signature = True
            else:
                picture = Image(BytesIO(image.blob), width=w * scale, height=h * scale)
                picture.hAlign = 'CENTER'
                pictures.append(picture)
        if markup:
            line = Paragraph(markup, style)
            if inline_signature:
                line.autoLeading = 'max'
            content.append(line)
        content.extend(pictures)
    return content


def _form_table(table):
    widths = [column.width for column in table.columns]
    widths = [width / sum(widths) * _BODY_WIDTH for width in widths]
    rows = []
    style = [('GRID', (0, 0), (-1, -1), .5, 'black'),
             ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
             ('LEFTPADDING', (0, 0), (-1, -1), 4),
             ('RIGHTPADDING', (0, 0), (-1, -1), 4)]
    for index, row in enumerate(table.rows):
        cells = []
        for _, group in groupby(row.cells, key=lambda cell: cell._tc):
            span = list(group)
            start, end = len(cells), len(cells) + len(span)
            cells.extend([_cell_content(span[0], sum(widths[start:end]))] + [''] * (len(span) - 1))
            if end > start + 1:
                style.append(('SPAN', (start, index), (end - 1, index)))
        rows.append(cells)
    return Table(rows, colWidths=widths, style=style)


def _picture_row(images):
    cell_width = _BODY_WIDTH / len(images)
    pictures = []
    for index, image in enumerate(images):
        if image is not None:
            scale = min((cell_width - _GAP) / image.width, 108 * mm / image.height)
            center = _MARGIN_X + (index + .5) * cell_width
            pictures.append((image, center, image.width * scale, image.height * scale))
    return pictures, len(images)


def _attachment_rows(bundle, settings, stack, meal):
    invoices = panel_images([m for m in bundle.materials if m.kind == '电子发票'], settings, stack)
    evidence = [m for m in bundle.materials if m.kind not in (*STANDALONE_SHEETS, '电子发票')]
    rows = [_picture_row(invoices[i:i + 3]) for i in range(0, len(invoices), 3)]
    if meal:
        evidence.extend(m for m in bundle.materials if m.kind == '签领表')
        rows.extend(_picture_row(pair) for pair in meal_rows(evidence, settings, stack))
    else:
        pages = panel_images(evidence, settings, stack)
        rows.extend(_picture_row(pages[i:i + 3]) for i in range(0, len(pages), 3))
    return rows


def _picture_height(pictures):
    return max((height for _, _, _, height in pictures), default=12)


def _draw_page(canvas, rows, top, available, scale):
    for pictures, columns in rows:
        height = _picture_height(pictures) * scale
        frame_height = height + 2 * _FRAME_PADDING
        used = frame_height + _GAP
        if used > available + .01:
            break
        for image, center, width, image_height in pictures:
            width, image_height = width * scale, image_height * scale
            canvas.drawImage(ImageReader(image), center - width / 2, top - _FRAME_PADDING - (height + image_height) / 2,
                             width=width, height=image_height)
        bottom = top - frame_height
        canvas.saveState()
        canvas.setStrokeColorRGB(0, 0, 0)
        canvas.setLineWidth(.5)
        canvas.setDash(3, 2)
        canvas.rect(_MARGIN_X, bottom, _BODY_WIDTH, frame_height)
        for index in range(1, columns):
            x = _MARGIN_X + index * _BODY_WIDTH / columns
            canvas.line(x, bottom, x, top)
        canvas.restoreState()
        top -= used
        available -= used
        yield top


def build_pdf(bundle: Bundle, sequence: str, destination: Path, settings: Settings, details=None) -> Path:
    """Use the populated built-in form and the original evidence, without a Word export."""
    document = prepare_document(bundle, sequence, None, details)
    meal = any(p.text.strip() == MEAL_PANEL for p in document.paragraphs)
    header, declaration = map(_form_table, document.tables)
    content = BytesIO()
    canvas = Canvas(content, pagesize=A4)
    canvas.setTitle(f'{sequence}. {bundle.item}')
    _, header_height = header.wrapOn(canvas, _BODY_WIDTH, _BODY_HEIGHT)
    _, declaration_height = declaration.wrapOn(canvas, _BODY_WIDTH, _BODY_HEIGHT)
    available = _BODY_HEIGHT - header_height - declaration_height - _GAP
    if available < 0:
        raise ValueError('表头与签字栏内容超过一页，请缩短填写内容')
    top = A4[1] - _MARGIN_Y
    header.drawOn(canvas, _MARGIN_X, top - header_height)
    top -= header_height + _GAP
    with ExitStack() as stack:
        rows = _attachment_rows(bundle, settings, stack, meal)
        images_height = sum(_picture_height(pictures) for pictures, _ in rows)
        spacing = len(rows) * (_GAP + 2 * _FRAME_PADDING)
        scale = min(1, (available - spacing) / images_height) if images_height else 1
        # Do not shrink long sets of evidence below a readable size just to fit one page.
        first_page = list(_draw_page(canvas, rows, top, available, scale if scale >= .8 else 1))
        declaration.drawOn(canvas, _MARGIN_X, (first_page[-1] if first_page else top) - declaration_height)
        remaining = rows[len(first_page):]
        while remaining:
            canvas.showPage()
            placed = list(_draw_page(canvas, remaining, A4[1] - _MARGIN_Y, _BODY_HEIGHT, 1))
            remaining = remaining[len(placed):]
        canvas.save()
    with destination.open('xb') as stream:
        stream.write(content.getbuffer())
    return destination
