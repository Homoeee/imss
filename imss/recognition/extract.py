"""文件内容识别与本地文字提取；扩展名不可靠时按文件头判断格式。

PDF 优先读取带坐标的文字层，缺少文字时逐页 OCR；Word 图片需经
LibreOffice 渲染。PDFium 原生对象共用锁，避免并发访问导致崩溃。
"""
from contextlib import closing, contextmanager
import csv
import io
from pathlib import Path
import re
from subprocess import TimeoutExpired
import tempfile
import threading
import zipfile

from PIL import Image, ImageOps, ImageSequence
import pypdfium2 as pdfium

from imss.core.models import Settings
from imss.runtime.office import convert_office, find_executable, run_process
from imss.runtime.paths import bundled_tesseract, tessdata_path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
# PDFium is not thread-safe, even with separate documents. Image OCR can run
# concurrently, while all PDFium access in this process is serialized.
PDFIUM_LOCK = threading.RLock()


class Extraction(tuple):
    """兼容原来的 text, issues 解包，同时携带可序列化的页面文字框。"""
    def __new__(cls, text, issues, layout=None):
        result = super().__new__(cls, (text, issues))
        result.layout = layout or []
        return result


def detect_extension(path: Path) -> str:
    """返回实际格式，允许把扩展名错误的 .tmp 图片作为正常材料处理。"""
    with path.open("rb") as stream:
        header = stream.read(1024)
    if b"%PDF-" in header:
        return ".pdf"
    if header.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" in archive.namelist():
                    return ".docx"
        except zipfile.BadZipFile:
            pass
    if header.startswith(b"\xd0\xcf\x11\xe0") and path.suffix.lower() in (".doc", ".tmp", ".temp"):
        return ".doc"
    try:
        with Image.open(path) as image:
            return {"JPEG": ".jpg", "PNG": ".png", "TIFF": ".tiff", "BMP": ".bmp",
                    "GIF": ".gif", "WEBP": ".webp"}.get(image.format, "")
    except (OSError, ValueError):
        return ""


def tesseract_path(settings: Settings) -> str:
    return find_executable(settings.tesseract, ("tesseract",), bundled=bundled_tesseract())


def ocr_image(image: Image.Image, settings: Settings, _psm=3) -> tuple[str, list[str]]:
    executable = tesseract_path(settings)
    if image.width * image.height > settings.max_pixels:
        raise ValueError("图片超过 max_pixels 限制")
    with tempfile.TemporaryDirectory(prefix="models-ocr-") as directory:
        path = Path(directory) / "page.png"
        prepared = ImageOps.grayscale(image)
        # Small chat-compressed invoices/screenshots in the reference are only
        # 200–600 px wide. Upscaling helps segmentation; it cannot restore detail.
        factor = min(4.0, max(1.0, 1000 / min(prepared.size)))
        factor = min(factor, (settings.max_pixels / (prepared.width * prepared.height)) ** .5)
        if factor > 1:
            prepared = prepared.resize((int(prepared.width * factor), int(prepared.height * factor)), Image.Resampling.LANCZOS)
        prepared.save(path)
        args = [executable, str(path), "stdout", "-l", settings.languages, "--psm", str(_psm)]
        tessdata = tessdata_path(settings)
        if tessdata:
            args.extend(["--tessdata-dir", str(tessdata)])
        # Set the renderer directly: a custom tessdata directory may contain
        # only models and none of Tesseract's optional configs/tsv files.
        args.extend(["-c", "tessedit_create_txt=0", "-c", "tessedit_create_tsv=1"])
        result = run_process(args, settings.ocr_timeout)
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"OCR 失败，请检查语言模型与 tesseract 路径: {detail}")
        rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8", errors="replace")), delimiter="\t")
        if not {"text", "conf", "line_num"}.issubset(rows.fieldnames or []):
            raise RuntimeError("Tesseract 未返回 TSV 数据，请检查本地 OCR 安装")
        lines: dict[tuple, list[str]] = {}
        confidence = []
        layout = []
        for row in rows:
            word = row.get("text", "").strip()
            if not word:
                continue
            key = tuple(row.get(field) for field in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault(key, []).append(word)
            score = float(row.get("conf", "-1"))
            if score >= 0:
                confidence.append(score)
            if all(row.get(field) is not None for field in ('left', 'top', 'width', 'height')):
                left, top, width, height = (float(row[field]) for field in ('left', 'top', 'width', 'height'))
                layout.append(dict(text=word, confidence=score, page=1, engine='tesseract',
                                   box=[left / prepared.width, top / prepared.height,
                                        (left + width) / prepared.width, (top + height) / prepared.height]))
        text = "\n".join(" ".join(words) for words in lines.values())
        issues = []
        if not text.strip():
            issues.append("OCR 未提取到文字")
        elif confidence and sum(confidence) / len(confidence) < 55:
            issues.append("OCR 平均置信度偏低，请核对原件")
        # 稀疏排版重试只在首轮明显不足时进行；不拼接两轮文字，以免重复金额。
        if _psm == 3 and (len(re.sub(r'\s', '', text)) < settings.ocr_min_chars or issues):
            try:
                retry = ocr_image(image, settings, _psm=11)
                def quality(value, boxes):
                    scores = [b['confidence'] for b in boxes if b['confidence'] >= 0]
                    return len(re.sub(r'\s', '', value)) * (sum(scores) / len(scores) if scores else 50)
                if quality(retry[0], retry.layout) > quality(text, layout) * 1.1:
                    return retry
            except (RuntimeError, OSError, TimeoutExpired):
                pass  # 首轮仍可使用，重试失败不能丢掉已经读取的证据。
        return Extraction(text, issues, layout)


def image_pages(path: Path, settings: Settings):
    with Image.open(path) as original:
        count = getattr(original, "n_frames", 1)
        if count > settings.max_pages:
            raise ValueError(f"图片帧数超过 max_pages={settings.max_pages}")
        for frame in ImageSequence.Iterator(original):
            if frame.width * frame.height > settings.max_pixels:
                raise ValueError("图片超过 max_pixels 限制")
            frame = ImageOps.exif_transpose(frame)
            if frame.mode in ("RGBA", "LA") or "transparency" in frame.info:
                rgba = frame.convert("RGBA")
                white = Image.new("RGBA", rgba.size, "white")
                white.alpha_composite(rgba)
                yield white.convert("RGB")
            else:
                yield frame.convert("RGB")


def check_pdf(document, settings):
    if len(document) > settings.max_pages:
        raise ValueError(f"PDF 页数超过 max_pages={settings.max_pages}")


def render_page(page, settings):
    width, height = page.get_size()
    scale = settings.dpi / 72
    if width * height * scale * scale > settings.max_pixels:
        raise ValueError("PDF 渲染像素超过 max_pixels 限制")
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()


def pdf_pages(path: Path, settings: Settings):
    with PDFIUM_LOCK, pdfium.PdfDocument(path) as document:
        check_pdf(document, settings)
        for index in range(len(document)):
            with closing(document[index]) as page:
                yield render_page(page, settings)


@contextmanager
def as_pdf(path: Path, extension: str, settings: Settings):
    if extension == ".pdf":
        yield path
    else:
        with tempfile.TemporaryDirectory(prefix="models-pages-") as directory:
            yield convert_office(path, Path(directory) / "document.pdf", settings)


def material_pages(path: Path, extension: str, settings: Settings):
    if extension in IMAGE_EXTENSIONS:
        yield from image_pages(path, settings)
    else:
        with as_pdf(path, extension, settings) as pdf:
            yield from pdf_pages(pdf, settings)


def extract_pdf(path: Path, settings: Settings) -> tuple[str, list[str]]:
    """返回逐页文字及识别警告；不把文字层不足的页面默认为识别成功。"""
    texts, issues, layout = [], [], []
    with PDFIUM_LOCK, pdfium.PdfDocument(path) as document:
        check_pdf(document, settings)
        for index in range(len(document)):
            with closing(document[index]) as page:
                with closing(page.get_textpage()) as textpage:
                    page_layout = []
                    text = pdf_layout_text(textpage, page_layout, page.get_size())
                visible = re.sub(r"\s", "", text)
                if len(visible) < settings.ocr_min_chars:
                    try:
                        image = render_page(page, settings)
                        try:
                            result = ocr_image(image, settings)
                            ocr, warnings = result
                        finally:
                            image.close()
                        if len(re.sub(r"\s", "", ocr)) > len(visible):
                            text = ocr
                            page_layout = getattr(result, 'layout', [])
                        issues.extend(f"第{index + 1}页: {warning}" for warning in warnings)
                    except Exception as exc:
                        issues.append(f"第{index + 1}页 OCR: {exc}")
                texts.append(text)
                layout.extend(dict(block, page=index + 1) for block in page_layout)
    return Extraction("\n".join(texts), issues, layout)


def pdf_layout_text(textpage, layout=None, size=None) -> str:
    """Recover visual lines instead of trusting PDF object insertion order.

    Electronic VAT PDFs commonly store labels first and all values afterwards.
    Grouping character boxes makes gross totals and invoice numbers adjacent again.
    """
    characters = []
    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char.strip() and char != " ":
            continue
        # Font boxes preserve punctuation baselines. Tight dot boxes otherwise
        # fall onto a separate line and can silently turn 20.30 into 20.00.
        left, bottom, right, top = textpage.get_charbox(index, loose=True)
        if top <= bottom:
            continue
        if layout is not None and size and char.strip():
            layout.append(dict(text=char, confidence=None, page=1, engine='pdf-text',
                               box=[left / size[0], 1 - top / size[1], right / size[0], 1 - bottom / size[1]]))
        characters.append((left, (bottom + top) / 2, right, top - bottom, char))
    if not characters:
        return textpage.get_text_range()
    lines = []
    for char in sorted(characters, key=lambda c: (-c[1], c[0])):
        target = next((line for line in reversed(lines[-8:]) if abs(line[0] - char[1]) <= max(1.8, min(line[1], char[3]) * .55)), None)
        if target is None:
            lines.append([char[1], char[3], [char]])
        else:
            target[2].append(char)
    output = []
    for _, _, chars in sorted(lines, key=lambda line: -line[0]):
        last = None
        words = []
        for left, _, right, height, char in sorted(chars, key=lambda c: c[0]):
            if last is not None and left - last > max(1.5, height * .4):
                words.append(" ")
            words.append(char)
            last = right
        output.append("".join(words))
    return "\n".join(output)


def extract_docx(path: Path, settings: Settings) -> tuple[str, list[str]]:
    # Include tables, text boxes, headers and footers by reading all Word text nodes.
    from lxml import etree

    texts, issues, layout = [], [], []
    with zipfile.ZipFile(path) as archive:
        for name in sorted(archive.namelist(), key=lambda name: (name != 'word/document.xml', name)):
            if re.fullmatch(r"word/(document|header\d+|footer\d+)\.xml", name):
                parser = etree.XMLParser(resolve_entities=False, no_network=True)
                root = etree.fromstring(archive.read(name), parser=parser)
                def node_text(node):
                    return ''.join(node.xpath('.//w:t/text()', namespaces={
                        'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}))
                def read_nodes(node):
                    # 一行表格一次读取，保留空单元格和列位置，避免“合计/数量/金额”串列。
                    if node.tag.endswith('}tbl'):
                        for row in node.findall('./{*}tr'):
                            cells = [' '.join(node_text(p) for p in cell.findall('.//{*}p'))
                                     for cell in row.findall('./{*}tc')]
                            if any(cells):
                                texts.append('\t'.join(cells))
                    elif node.tag.endswith('}p'):
                        if text := node_text(node):
                            texts.append(text)
                    else:
                        for child in node:
                            read_nodes(child)
                read_nodes(root)
        media = [name for name in archive.namelist() if name.startswith("word/media/") and not name.endswith("/")]
        if len(media) > settings.max_pages:
            raise ValueError("Word 嵌入图片数量超过 max_pages 限制")
        for name in media:
            try:
                with Image.open(io.BytesIO(archive.read(name))) as embedded:
                    image = ImageOps.exif_transpose(embedded).convert("RGB")
                    try:
                        result = ocr_image(image, settings)
                        text, warnings = result
                    finally:
                        image.close()
                    texts.append(text)
                    layout.extend(dict(block, page=media.index(name) + 1) for block in getattr(result, 'layout', []))
                    issues.extend(warnings)
            except Exception as exc:
                issues.append(f"Word 内嵌图片 {name}: {exc}")
    return Extraction("\n".join(texts), issues, layout)


def extract(path: Path, extension: str, settings: Settings) -> tuple[str, list[str]]:
    if extension == ".pdf":
        return extract_pdf(path, settings)
    if extension == ".docx":
        return extract_docx(path, settings)
    if extension == ".doc":
        with as_pdf(path, extension, settings) as pdf:
            return extract_pdf(pdf, settings)
    texts, issues, layout = [], [], []
    for index, image in enumerate(image_pages(path, settings)):
        try:
            result = ocr_image(image, settings)
            text, warnings = result
            texts.append(text)
            layout.extend(dict(block, page=index + 1) for block in getattr(result, 'layout', []))
            issues.extend(f"第{index + 1}帧: {warning}" for warning in warnings)
        finally:
            image.close()
    return Extraction("\n".join(texts), issues, layout)
