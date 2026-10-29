"""用户可见目录约定：输入旁仅创建报销材料、核对清单两个目录。"""
from pathlib import Path
import re
import shutil
import stat

EVIDENCE_DIR = '原始材料'


def default_output(root: Path) -> Path:
    root = root.resolve()
    return root.parent / '报销材料'


def default_records(output: Path) -> Path:
    output = output.resolve()
    return output.parent / '核对清单'


def managed_path(output: Path, name: str) -> Path:
    """报告只允许根目录文件或“原始材料/文件名”，兼容旧版平铺成果。

    路径将用于覆盖、删除或摘要检查，不能接受越界路径、链接或额外子目录。
    报告统一使用正斜杠保存相对路径，避免 Windows 分隔符和绝对路径混用。
    """
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('输出记录含无效文件路径')
    relative = Path(name)
    if (relative.is_absolute() or relative.as_posix() != name
            or len(relative.parts) not in (1, 2)
            or (len(relative.parts) == 2 and relative.parts[0] != EVIDENCE_DIR)
            or any(part in ('.', '..') or part.endswith((' ', '.'))
                   or re.search(r'[<>:"|?*\x00-\x1f]', part) for part in relative.parts)):
        raise ValueError('输出记录含无效文件路径: ' + name)
    path = output / relative
    for component in (output, *[output.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts) + 1)]):
        if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
            raise ValueError('输出路径不能使用符号链接或目录联接: ' + str(component))
    try:
        path.resolve().relative_to(output.resolve())
    except ValueError as exc:
        raise ValueError('输出路径越过报销材料目录: ' + name) from exc
    return path


def validate_layout(root: Path, output: Path, records: Path):
    """固定位置和名称，防止原件与成果混在一起或额外生成目录。"""
    if root == output:
        raise ValueError('输入和输出不能同目录')
    if root.name in ('报销材料', '核对清单'):
        raise ValueError('请选择原始输入目录，不能使用报销材料或核对清单目录作为输入')
    if output != default_output(root) or records != default_records(output):
        raise ValueError('输出必须为输入同级的“报销材料”，中间记录必须为同级的“核对清单”')


def reset_scan_directories(root: Path, output: Path, records: Path):
    """新扫描清空两个固定输出；先检查全部路径，再删除，绝不递归跟随链接。

    build/recheck 不调用此函数，核对后的继续处理必须保留清单和成果。
    Windows 的目录联接也属于重解析点，不能当成普通附件目录递归删除。
    """
    root = root.resolve()
    targets = (output, records)
    validate_layout(root, output, records)
    for target in targets:
        # 每个最终绝对路径必须仍是输入旁的固定目录，并且不能包含输入。
        if target.resolve() != root.parent / target.name or root.is_relative_to(target.resolve()):
            raise ValueError('拒绝清理越界或包含输入的输出路径: ' + str(target))
        pending = [target]
        while pending:
            path = pending.pop()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                raise ValueError('输出目录含符号链接或目录联接，请移除链接后重试: ' + str(path))
            if path.is_dir():
                pending.extend(path.iterdir())
    for target in targets:
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            target.mkdir(parents=True)
        except PermissionError as exc:
            raise PermissionError('无法清理输出，请关闭占用文件的 Excel、Word 或 PDF 程序后重试: ' + str(target)) from exc
