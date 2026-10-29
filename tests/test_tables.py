"""匿名标题、表格及清单回归：人员表独立输出，清单金额不能从单价猜。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from PIL import Image

from imss.core.models import Material, Settings
from imss.core.matching import group_materials
from imss.core.candidates import pairing_candidates
from imss.output.review import read_review
from imss.recognition.extract import Extraction, extract_docx
from imss.recognition.fields import extract_amount
from imss.recognition.refine import refine_missing_amount
from imss.recognition.rules import recognize
from imss.workflow import scan, build
from tests.test_core import text_pdf


def read(text, **kwargs):
    return recognize(Material('anonymous.png', 'sample', '.png', text=text, **kwargs))


class TableRecognitionTests(unittest.TestCase):
    def test_sheet_titles_do_not_require_purchase_fields(self):
        for kind, title in [('签到表', '迎新交流活动签到表'), ('签领表', '迎新活动奖品签领表'),
                            ('签到表', '培训活动签到登记表'), ('签领表', '工作餐签收表')]:
            with self.subTest(title=title):
                m = read(title + '\n序号 姓名 学号 签名\n1 张三 SA00000001')
                self.assertEqual(m.kind, kind)
                self.assertEqual(m.title, title)
                self.assertFalse(m.amount)
                self.assertFalse(m.issues)

    def test_split_sheet_title_keeps_activity(self):
        m = read('附件1\n迎新交流活动\n签 到 表\n姓名 学号 签名')
        self.assertEqual((m.kind, m.title), ('签到表', '迎新交流活动签到表'))

    def test_invoice_or_order_mention_does_not_become_a_sheet(self):
        self.assertEqual(read('电子发票\n物品名称：奖品\n备注：请附奖品签领表\n小写￥50').kind, '电子发票')
        self.assertEqual(read('订单详情\n商品名称：签到表\n实付款￥50').kind, '购买记录')
        self.assertEqual(read('订单详情\n商品名称：采购清单\n实付款￥50').kind, '购买记录')
        self.assertEqual(read('电子发票\n详见销售清单\n小写￥50').kind, '电子发票')

    def test_purchase_list_title_and_multiple_items(self):
        m = read('活动物资采购明细表\n品名\t数量\t单价\t金额\n打印纸\t2\t10\t20\n水杯\t3\t5\t15\n合计\t5\t\t35')
        self.assertEqual((m.kind, m.item, m.amount), ('购买清单', '活动物资采购明细表', '35.00'))
        self.assertFalse(m.issues)

    def test_list_structure_without_standard_title(self):
        m = read('迎新活动物资\n品名\t数量\t单价\t金额\n打印纸\t2\t10\t20\n合计\t\t\t20')
        self.assertEqual((m.kind, m.amount), ('购买清单', '20.00'))

    def test_list_total_labels_and_wrapped_amount(self):
        for ending in ('以上合计：13194元', '总金额（元）：13194', '总计\n￥13,194.00',
                       '金额合计（小写）13194元', '合计（大写）壹万叁仟元（小写）13194元'):
            with self.subTest(ending=ending):
                self.assertEqual(extract_amount('活动用品制作清单\n' + ending, '购买清单'), ('13194.00', []))

    def test_subtotal_does_not_override_grand_total(self):
        self.assertEqual(extract_amount('采购清单\n本页合计100\n总计300', '购买清单'), ('300.00', []))
        self.assertEqual(extract_amount('采购清单\n打印纸总价20元\n水杯总价15元\n总计35元', '购买清单'), ('35.00', []))

    def test_conflicting_totals_stay_blank(self):
        value, issues = extract_amount('采购清单\n合计100\n总额200', '购买清单')
        self.assertFalse(value)
        self.assertIn('冲突', issues[0])

    def test_never_choose_last_line_unit_price(self):
        for text in ('采购清单\n打印纸 单价10元 数量3', '采购清单\n品名 数量 单价\n打印纸 3 10',
                     '费用清单\n总额\n日期2026-10-24'):
            with self.subTest(text=text):
                self.assertFalse(extract_amount(text, '购买清单')[0])

    def test_bare_final_amount_requires_review(self):
        value, issues = extract_amount('采购清单\n打印纸2份\n￥35.00', '购买清单')
        self.assertEqual(value, '35.00')
        self.assertTrue(issues)

    def test_blank_total_can_only_suggest_sum_of_explicit_amount_column(self):
        value, issues = extract_amount('餐费清单\n日期\t份数\t单价(元)\t总价(元)\n晚餐\t25\t50\t1250\n合计', '购买清单')
        self.assertEqual(value, '1250.00')
        self.assertIn('按表格金额列汇总', issues[0])
        value, _ = extract_amount('餐费清单\n日期\t份数\t单价(元)\t总价(元)\n晚餐\t25\t50\t?\n合计', '购买清单')
        self.assertFalse(value)

    def test_list_title_and_total_do_not_auto_confirm_amount_candidate(self):
        invoice = Material('i.png', 'i', '.png', kind='电子发票', item='活动用品', amount='35.00')
        listing = read('活动用品采购清单\n打印纸20元\n水杯15元\n总计35元')
        proposals = pairing_candidates([invoice, listing])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]['amount'], '35.00')
        self.assertFalse(listing.confirmed)
        listing.group = invoice.group = 'a'
        bundles, pending = group_materials([invoice, listing])
        self.assertFalse(pending)
        self.assertFalse(bundles[0].issues)

    def test_visual_columns_recover_total_without_using_quantity(self):
        blocks = []
        for y, cells in [(.1, ['品名', '数量', '单价', '金额']),
                         (.2, ['打印纸', '2', '10', '20']), (.3, ['合计', '2', '', '20'])]:
            for x, text in zip((.05, .4, .6, .8), cells):
                if text:
                    blocks.append(dict(text=text, box=[x, y, x + .08, y + .02],
                                       confidence=95, page=1, engine='test'))
        m = read('采购清单\n品名 数量 单价 金额\n打印纸 2 10 20\n合计2 20', layout=blocks)
        self.assertEqual(m.amount, '20.00')
        self.assertFalse(m.issues)
        self.assertTrue(m.field_evidence['amount']['regions'])

    def test_docx_preserves_table_rows_and_merged_cells_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random.docx'
            doc = Document()
            doc.add_paragraph('活动用品采购清单')
            table = doc.add_table(rows=3, cols=4)
            for row, values in zip(table.rows, [('品名', '数量', '单价', '金额'),
                                               ('打印纸', '3', '10', '30'), ('合计', '', '', '30')]):
                for cell, value in zip(row.cells, values):
                    cell.text = value
            table.cell(2, 0).merge(table.cell(2, 2))
            doc.save(path)
            result = extract_docx(path, Settings())
            self.assertIn('打印纸\t3\t10\t30', result[0])
            # 合并后的总计行列数更少，紧随合计的唯一金额仍可读取。
            m = read(result[0])
            self.assertEqual(m.amount, '30.00')
            self.assertFalse(m.issues)

    def test_local_list_ocr_requires_label_and_preserves_original_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'list.png'
            Image.new('RGB', (1000, 1600), 'white').save(path)
            m = read('采购清单\n打印纸2份')
            block = dict(text='合计35元', page=1, confidence=90, engine='test', box=[.1, .5, .7, .6])
            with patch('imss.recognition.refine.ocr_image', return_value=Extraction('合计35元', [], [block])):
                refine_missing_amount(path, m, Settings())
            self.assertEqual(m.amount, '35.00')
            self.assertGreater(m.layout[-1]['box'][1], .45)
            self.assertTrue(any('局部OCR' in issue for issue in m.issues))
            m = read('采购清单\n打印纸2份')
            with patch('imss.recognition.refine.ocr_image', return_value=Extraction('￥35.00', [], [])):
                refine_missing_amount(path, m, Settings())
            self.assertFalse(m.amount)


class StandaloneSheetWorkflowTests(unittest.TestCase):
    def test_sheets_export_without_invoice_and_keep_original_pdf_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.mkdir()
            texts = {'a.pdf': '迎新活动签到表\n姓名 学号 签名',
                     'b.pdf': '迎新活动奖品签收表\n姓名 领取物品 签名'}
            for name in texts:
                text_pdf(source / name)
            # PDF 内容须不同，避免将测试中的两个表当成重复原件。
            with (source / 'b.pdf').open('ab') as stream:
                stream.write(b'\n% second original')
            originals = {name: (source / name).read_bytes() for name in texts}
            with patch('imss.workflow.extract', side_effect=lambda path, *_: (texts[path.name],
                       ['第1页: OCR 平均置信度偏低，请核对原件'] if path.name == 'b.pdf' else [])):
                manifest = scan(source, None, Settings(workers=1))
            from openpyxl import load_workbook
            workbook = load_workbook(manifest.with_suffix('.xlsx'))
            for cells in workbook.active.iter_rows(min_row=5):
                cells[7].value = '已校验'
            workbook.save(manifest.with_suffix('.xlsx'))
            workbook.close()
            report = build(manifest, skip_pdf=True)
            self.assertFalse(report['pending'])
            self.assertEqual({entry['kind'] for entry in report['generated']}, {'签到表', '签领表'})
            output = Path(report['output'])
            self.assertFalse(any(p.is_dir() for p in output.iterdir()))
            for entry in report['generated']:
                self.assertEqual((output / entry['pdf']).read_bytes(), originals[entry['sources'][0]])
                self.assertNotIn('签收表签领表', entry['pdf'])
            self.assertFalse(any('没有唯一匹配' in row['issues'] for row in read_review(manifest.parent / '核对清单.xlsx')))
            rebuilt = build(manifest, skip_pdf=True)
            self.assertFalse(rebuilt['pending'])
            self.assertEqual(len(list(output.glob('*.pdf'))), 2)
