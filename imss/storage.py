"""扫描状态序列化、文件摘要和原件校验；不处理表格展示。"""
import hashlib
import json
from pathlib import Path

from imss.core.models import Material, inside



def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value):
    """同目录临时写入后替换，避免中断把已保存的扫描状态截断。"""
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_sources(root: Path, materials: list[Material]):
    """生成前逐份比对原件摘要与路径；用户核对的必须仍是同一份原件。"""
    if not root.is_dir():
        raise ValueError(f"输入目录不存在: {root}")
    seen = set()
    for material in materials:
        path = root / material.source
        if path.is_symlink() or not inside(path, root) or not path.is_file():
            raise ValueError(f"材料不存在或路径越界: {material.source}")
        if material.source in seen:
            raise ValueError(f"重复材料路径: {material.source}")
        seen.add(material.source)
        if digest_file(path) != material.digest:
            raise ValueError(f"扫描后原件发生变化，请重新扫描: {material.source}")
