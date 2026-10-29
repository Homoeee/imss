"""Local office conversion, with isolated profiles and no shell invocation."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

from imss.core.models import Settings
from imss.runtime.paths import bundled_soffice, vendor_path


def find_executable(configured: str, names: tuple[str, ...],
                    bundled: tuple[str, ...] = ()) -> str:
    if configured:
        path = vendor_path(configured)
        if path.is_file():
            return str(path)
        raise RuntimeError(f"程序不存在: {configured}")
    for candidate in bundled:
        path = vendor_path(candidate)
        if path.is_file():
            return str(path)
    raise RuntimeError(f"项目 vendor 目录缺少 {names[0]}，请补齐项目内组件")


def soffice_path(settings: Settings) -> str:
    return find_executable(settings.soffice, ("soffice",), bundled=bundled_soffice())


def run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.setdefault("OMP_THREAD_LIMIT", "1")
    if os.name == "nt":
        # LibreOffice's loader needs its own program directory to locate helpers.
        # Exclude third-party directories inherited from the host's PATH.
        windows = Path(environment.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join((str(Path(args[0]).resolve().parent),
                                                str(windows / "System32"), str(windows)))
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False,
                          env=environment,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def convert_office(source: Path, destination: Path, settings: Settings, extension: str = "pdf") -> Path:
    if settings.office_backend not in ("auto", "libreoffice"):
        raise RuntimeError("Office 转换仅支持项目内 LibreOffice，请将 office_backend 设为 libreoffice")
    executable = soffice_path(settings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖: {destination}")
    # One isolated conversion directory avoids output basename collisions and stale PDFs.
    with tempfile.TemporaryDirectory(prefix="models-office-") as directory:
        root = Path(directory)
        profile = root / "profile"
        profile.mkdir()
        # Disable macros in the private conversion profile.
        (profile / "user").mkdir()
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8")
        target = root / "converted"
        target.mkdir()
        result = run_process([executable, "-env:UserInstallation=" + profile.as_uri(),
                              "--headless", "--nologo", "--norestore", "--convert-to", extension,
                              "--outdir", str(target), str(source.resolve())], settings.office_timeout)
        converted = target / (source.stem + "." + extension)
        if result.returncode or not converted.is_file() or converted.stat().st_size == 0:
            detail = (result.stderr + result.stdout).decode("utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"LibreOffice 转换失败: {source.name}: {detail}")
        if extension == "pdf" and not converted.read_bytes()[:5] == b"%PDF-":
            raise RuntimeError(f"转换未生成有效 PDF: {source.name}")
        shutil.copy2(converted, destination)
    return destination

