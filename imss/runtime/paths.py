"""Project-local runtimes. Never discover programs or models on the host."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VENDOR_ROOT = PROJECT_ROOT / "vendor"


def vendor_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if not path.is_relative_to(VENDOR_ROOT.resolve()):
        raise RuntimeError(f"组件和模型必须位于项目 vendor 目录内: {value}")
    return path


def bundled_tesseract() -> tuple[str, ...]:
    names = ("tesseract.exe", "bin/tesseract.exe") if os.name == "nt" else ("bin/tesseract", "tesseract")
    return tuple(str(VENDOR_ROOT / "tesseract" / name) for name in names)


def bundled_soffice() -> tuple[str, ...]:
    if os.name == "nt":
        names = ("libreoffice/program/soffice.com", "libreoffice/program/soffice.exe",
                 "LibreOfficePortable/App/libreoffice/program/soffice.com",
                 "LibreOfficePortable/App/libreoffice/program/soffice.exe")
    else:
        names = ("libreoffice/program/soffice", "libreoffice/bin/soffice")
    return tuple(str(VENDOR_ROOT / name) for name in names)


def tessdata_path(settings) -> Path:
    local = vendor_path(settings.tessdata or str(VENDOR_ROOT / "tesseract" / "tessdata"))
    if not local.is_dir():
        raise RuntimeError(f"项目内 OCR 模型目录缺失: {local}")
    return local
