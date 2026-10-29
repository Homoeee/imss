"""新扫描重建固定输出，输入校验失败和危险链接不能触发删除。"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import stat
import unittest
from unittest.mock import patch

from docx import Document
from PIL import Image

from imss.core.models import Settings
from imss.output.workspace import reset_scan_directories
from imss.storage import digest_file
from imss.workflow import scan


class ScanResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.source = self.parent / '本次材料'
        self.source.mkdir()
        self.original = self.source / '发票.png'
        Image.new('RGB', (100, 100), 'white').save(self.original)
        self.original_hash = digest_file(self.original)
        self.output = self.parent / '报销材料'
        self.records = self.parent / '核对清单'
        for directory in (self.output, self.records):
            directory.mkdir()
            (directory / '旧文件.txt').write_text('旧内容', encoding='utf-8')

    def rescan(self, **kwargs):
        with patch('imss.workflow.extract', return_value=('电子发票\n物品名称：打印纸\n（小写）￥35.80', [])):
            return scan(self.source, None, Settings(workers=1), **kwargs)

    def assert_old_files_remain(self):
        for directory in (self.output, self.records):
            self.assertEqual((directory / '旧文件.txt').read_text(encoding='utf-8'), '旧内容')
        self.assertEqual(digest_file(self.original), self.original_hash)

    def test_nonempty_outputs_are_removed_and_recreated_with_fresh_review(self):
        nested = self.output / '原始材料' / '旧子目录'
        nested.mkdir(parents=True)
        (nested / '旧附件.pdf').write_bytes(b'old')
        sibling = self.parent / '其他批次'
        sibling.mkdir()
        (sibling / '保留.txt').write_text('保留', encoding='utf-8')
        first = self.rescan()
        self.assertEqual(first, self.records / '核对清单.json')
        self.assertFalse(list(self.output.iterdir()))
        self.assertEqual({p.name for p in self.records.iterdir()}, {'核对清单.json', '核对清单.xlsx'})
        self.assertEqual(len(json.loads(first.read_text(encoding='utf-8'))['materials']), 1)
        (self.output / '手工新增.txt').write_text('下次扫描同样清空', encoding='utf-8')
        (self.records / '旧核对记录.csv').write_text('旧核对', encoding='utf-8')
        self.rescan()
        self.assertFalse(list(self.output.iterdir()))
        self.assertEqual({p.name for p in self.records.iterdir()}, {'核对清单.json', '核对清单.xlsx'})
        self.assertEqual(digest_file(self.original), self.original_hash)
        self.assertEqual((sibling / '保留.txt').read_text(encoding='utf-8'), '保留')

    def test_invalid_template_does_not_clear_outputs(self):
        with self.assertRaisesRegex(ValueError, '模板必须'):
            self.rescan(template=self.parent / 'missing.docx')
        self.assert_old_files_remain()

    def test_template_inside_output_is_rejected_before_cleanup(self):
        template = self.output / '自定义模板.docx'
        Document().save(template)
        with self.assertRaisesRegex(ValueError, '模板不能放在即将清空'):
            self.rescan(template=template)
        self.assertTrue(template.is_file())
        self.assert_old_files_remain()

    def test_empty_input_does_not_clear_outputs(self):
        self.original.unlink()
        with self.assertRaisesRegex(ValueError, '没有材料'):
            self.rescan()
        self.assertTrue(all((d / '旧文件.txt').is_file() for d in (self.output, self.records)))

    def test_nonfixed_target_does_not_delete_any_directory(self):
        other = self.parent / '不应清空'
        other.mkdir()
        note = other / '保留.txt'
        note.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, '输出必须为输入同级'):
            reset_scan_directories(self.source, other, self.records)
        self.assertEqual(note.read_bytes(), b'keep')
        self.assert_old_files_remain()

    def test_nested_link_is_rejected_before_either_directory_is_deleted(self):
        link = self.records / '输入链接'
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'当前系统不允许创建符号链接: {exc}')
        with self.assertRaisesRegex(ValueError, '符号链接或目录联接'):
            self.rescan()
        self.assert_old_files_remain()

    @unittest.skipUnless(hasattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT'), '仅 Windows 有目录重解析点属性')
    def test_windows_reparse_directory_is_rejected_in_preflight(self):
        directory = self.records / '模拟目录联接'
        directory.mkdir()
        original_lstat = Path.lstat
        def lstat(path):
            info = original_lstat(path)
            if path == directory:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch('pathlib.Path.lstat', lstat):
            with self.assertRaisesRegex(ValueError, '符号链接或目录联接'):
                self.rescan()
        self.assert_old_files_remain()

    def test_locked_file_reports_close_app_instead_of_nonempty_error(self):
        with patch('imss.output.workspace.shutil.rmtree', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(PermissionError, '关闭占用文件'):
                self.rescan()
        self.assert_old_files_remain()

    def test_existing_files_at_fixed_output_paths_are_replaced_by_directories(self):
        for directory in (self.output, self.records):
            (directory / '旧文件.txt').unlink()
            directory.rmdir()
            directory.write_text('旧文件', encoding='utf-8')
        self.rescan()
        self.assertTrue(self.output.is_dir())
        self.assertTrue((self.records / '核对清单.xlsx').is_file())
        self.assertEqual(digest_file(self.original), self.original_hash)


if __name__ == '__main__':
    unittest.main()
