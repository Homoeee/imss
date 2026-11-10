"""Start project-local OCR tools without searching the host PATH."""

import os
from pathlib import Path
import subprocess

from imss.runtime.paths import vendor_path


def find_executable(configured: str, names: tuple[str, ...], bundled: tuple[str, ...] = ()) -> str:
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


def run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment.setdefault("OMP_THREAD_LIMIT", "1")
    if os.name == "nt":
        windows = Path(environment.get("SystemRoot", r"C:\Windows"))
        environment["PATH"] = os.pathsep.join((str(Path(args[0]).resolve().parent),
                                                str(windows / "System32"), str(windows)))
    return subprocess.run(args, capture_output=True, timeout=timeout, check=False,
                          env=environment,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
