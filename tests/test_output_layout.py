import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from docx import Document
from imss.core.models import Material, Settings

from imss.output.review import write_review
from imss.workflow import archive_materials, build, scan
from imss.core.models import Bundle
from imss.output.workspace import default_records, managed_path
from imss.storage import digest_file


class OutputLayoutTests(unittest.TestCase):
    def scan_pair(self, root):
        source = root / 'input'
        source.mkdir()
        texts = {'i.png': '电子发票\n物品名称：打印纸\n（小写）￥35.80',
                 'p.png': '交易成功\n物品名称：打印纸\n实付款：35.80'}
        for index, name in enumerate(texts):
            Image.new('RGB', (120 + index, 100), 'white').save(source / name)
        with patch('imss.workflow.extract', side_effect=lambda path, *_: (texts[path.name], [])):
            return scan(source, None, Settings(workers=1))

    def test_unmatched_evidence_stays_in_input_and_numbered_evidence_is_nested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, records_output = root / 'input', root / 'output', root / 'records'
            for path in (source, output, records_output):
                path.mkdir()
            materials = [Material('invoice.tmp', '', '.png', kind='电子发票', item='打印纸', amount='35.80'),
                         Material('copy.tmp', '', '.png', kind='电子发票', item='打印纸', amount='35.80', duplicate_of='invoice.tmp'),
                         Material('unknown.tmp', '', '', kind='未知'),
                         Material('order.tmp', '', '.png', kind='购买记录', item='未配对订单', amount='10.00')]
            for material in materials:
                (source / material.source).write_bytes(b'original evidence')
            bundle = Bundle('manual:24.2', '打印纸', '35.80', [materials[0]])
            archive_materials(materials, [bundle], {'manual:24.2': '01'}, source, output, records_output)
            self.assertEqual({p.name for p in output.iterdir()}, {'原始材料'})
            self.assertEqual({p.name for p in (output / '原始材料').iterdir()},
                             {'01.打印纸-35.80元-电子发票.png', '01.打印纸-35.80元-电子发票__02.png'})
            for material in materials[:2]:
                self.assertEqual(Path(material.archived).read_bytes(), (source / material.source).read_bytes())
            self.assertTrue(all(not m.archived for m in materials[2:]))
            self.assertFalse(any(records_output.iterdir()))
            self.assertFalse(any(p.is_dir() for p in (output / '原始材料').iterdir()))

    def test_export_pdf_updates_the_separate_processing_report(self):
        from main import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'input', root / '报销材料'
            source.mkdir()
            for index in range(2):
                Image.new('RGB', (120 + index, 100), 'white').save(source / f'{index}.png')
            texts = ['电子发票\n物品名称：打印纸\n（小写）￥35.80', '交易成功\n物品名称：打印纸\n实付款：35.80']
            with patch('imss.workflow.extract', side_effect=lambda path, ext, settings: (texts[int(path.stem)], [])):
                manifest = scan(source, output, Settings(workers=1))
            report = build(manifest, skip_pdf=True)
            self.assertIsNone(report['generated'][0]['pdf'])
            def convert(source, destination, settings):
                Image.new('RGB', (120, 100), 'white').save(destination, 'PDF')
                return destination
            with patch('imss.runtime.office.convert_office', side_effect=convert):
                self.assertEqual(main(['export-pdf', str(output)]), 0)
            report = json.loads((default_records(output) / '核对清单.json').read_text(encoding='utf-8'))['report']
            self.assertTrue(report['generated'][0]['pdf'])
            self.assertFalse(any('--skip-pdf' in issue for entry in report['warnings'] for issue in entry['issues']))
            self.assertFalse((output / '处理报告.json').exists())
            self.assertEqual(len([name for name in report['files'] if name.startswith('原始材料/')]), 2)
            self.assertTrue(all((output / name).is_file() for name in report['files']))
            # 补导 PDF 后，嵌套附件的摘要仍可用于重复 build。
            self.assertFalse(build(manifest, skip_pdf=True)['pending'])

    def test_shared_receipt_is_one_trailing_pdf_and_never_inside_words(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / 'input', root / '报销材料'
            source.mkdir()
            labels = [('a.png', '电子发票', '风扇', '49.34', '01.1'),
                      ('b.png', '购买记录', '风扇', '49.34', '01.1'),
                      ('c.png', '电子发票', '水杯', '103.00', '02'),
                      ('d.png', '购买记录', '水杯', '103.00', '02'),
                      ('e.png', '签领表', '风扇及水杯', '', '01.1|02'),
                      ('f.png', '情况说明', '活动经费', '', '')]
            records = []
            for index, (name, kind, item, amount, group) in enumerate(labels):
                Image.new('RGB', (120 + index, 100), 'white').save(source / name)
                records.append(Material(name, '', '.png', kind=kind, item=item, amount=amount, group=group))
            with patch('imss.workflow.extract', return_value=('待核对', [])):
                manifest = scan(source, output, Settings(workers=1))
            review = root / 'review.csv'
            write_review(review, records)
            with review.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            with review.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0])
                writer.writeheader()
                writer.writerows(dict(row, confirmed='1') for row in rows)
            details = root / 'details.json'
            details.write_text(json.dumps({key: {'category': '奖品'} for key in ('01.1', '02')}), encoding='utf-8')
            report = build(manifest, review=review, details_path=details, skip_pdf=True)
            self.assertFalse(report['pending'])
            self.assertEqual([v['kind'] for v in report['generated']], ['报销材料', '报销材料', '签领表', '情况说明'])
            self.assertEqual(report['generated'][2]['sequence'], '03')
            self.assertEqual(report['generated'][2]['sources'], ['e.png'])
            self.assertEqual({p.suffix for p in output.iterdir() if p.is_file()}, {'.docx', '.pdf'})
            self.assertEqual({p.name for p in output.iterdir() if p.is_dir()}, {'原始材料'})
            self.assertEqual({p.name for p in (output / '原始材料').glob('*元-*')},
                             {'01.风扇-49.34元-电子发票.png', '01.风扇-49.34元-购买记录.png',
                              '02.水杯-103.00元-电子发票.png', '02.水杯-103.00元-购买记录.png'})
            self.assertTrue(all(not m['archived'] for m in report['materials'] if m['kind'] in ('签领表', '情况说明')))
            self.assertTrue((default_records(output) / '核对清单.json').is_file())
            for entry in report['generated'][:2]:
                self.assertEqual(len(entry['sources']), 2)
                document = Document(output / entry['word'])
                self.assertEqual(len(document.inline_shapes), 3)  # promise, invoice, order

    def test_processing_records_cannot_pollute_submission_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.mkdir()
            output = root / '报销材料'
            with self.assertRaisesRegex(ValueError, '输出必须为输入同级'):
                scan(source, output, Settings(), records_output=output / 'records')
            self.assertFalse(output.exists())

    def test_rebuild_migrates_managed_flat_evidence_and_keeps_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.scan_pair(root)
            report = build(manifest, skip_pdf=True)
            output = Path(report['output'])
            state = json.loads(manifest.read_text(encoding='utf-8'))
            # 模拟此前平铺版本的输出与摘要，用户无需重新扫描或修改核对表。
            old_files = {}
            for name, digest in report['files'].items():
                previous_name = Path(name).name
                if name.startswith('原始材料/'):
                    (output / name).replace(output / previous_name)
                old_files[previous_name] = digest
            (output / '原始材料').rmdir()
            state['report']['files'] = old_files
            manifest.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            note = output / '用户备注.txt'
            note.write_text('保留用户内容', encoding='utf-8')
            rebuilt = build(manifest, skip_pdf=True)
            self.assertFalse(rebuilt['pending'])
            self.assertFalse(list(output.glob('*元-*')))
            evidence = list((output / '原始材料').iterdir())
            self.assertEqual(len(evidence), 2)
            self.assertEqual(note.read_text(encoding='utf-8'), '保留用户内容')
            self.assertTrue(all(name.startswith('原始材料/') for name in rebuilt['files'] if '元-' in name))
            for material in rebuilt['materials']:
                if material['archived']:
                    self.assertEqual(Path(material['archived']).parent, output / '原始材料')
                    self.assertEqual(digest_file(Path(material['archived'])), material['digest'])

    def test_modified_nested_evidence_blocks_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.scan_pair(Path(directory))
            report = build(manifest, skip_pdf=True)
            evidence = Path(next(m['archived'] for m in report['materials'] if m['archived']))
            evidence.write_bytes(b'user edited evidence')
            with self.assertRaisesRegex(ValueError, '人工修改'):
                build(manifest, skip_pdf=True)
            self.assertEqual(evidence.read_bytes(), b'user edited evidence')

    def test_unmanaged_nested_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.scan_pair(root)
            output = root / '报销材料'
            archive = output / '原始材料'
            archive.mkdir()
            collision = archive / '01.打印纸-35.80元-电子发票.png'
            collision.write_bytes(b'user file')
            with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
                build(manifest, skip_pdf=True)
            self.assertEqual(collision.read_bytes(), b'user file')
            self.assertFalse(list(output.glob('*.docx')))

    def test_unmanaged_file_cannot_be_used_as_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.scan_pair(root)
            collision = root / '报销材料' / '原始材料'
            collision.write_bytes(b'user file')
            with self.assertRaisesRegex(ValueError, '同名文件'):
                build(manifest, skip_pdf=True)
            self.assertEqual(collision.read_bytes(), b'user file')

    def test_managed_paths_reject_traversal_and_extra_subdirectories(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for name in ('../outside', '原始材料/../../outside', '/outside', 'C:/outside',
                         '其他目录/file.pdf', '原始材料/a/b.pdf', '原始材料\\file.pdf', './file.pdf'):
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, '无效文件路径'):
                    managed_path(output, name)
            self.assertEqual(managed_path(output, '原始材料/01.发票.pdf'), output / '原始材料' / '01.发票.pdf')
            self.assertEqual(managed_path(output, '01.旧版附件.pdf'), output / '01.旧版附件.pdf')
