"""小图发票的结构分类、表格定位及金额建议；不以原文件名提供答案。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from imss.core.models import Material, Settings
from imss.recognition.extract import Extraction, image_pages
from imss.recognition.invoices import has_invoice_structure, invoice_regions, local_gross
from imss.recognition.refine import refine_missing_amount
from imss.recognition.rules import recognize, classify


def invoice_text():
    return ('电子 发\n开票日期2026年05月30日\n社会信用代码12100000485001086E\n'
            '销售方91340111MA00000001\n*文具*白板笔\n￥30.91 税额0.31')


class InvoiceStructureTests(unittest.TestCase):
    def test_partial_title_with_independent_tax_ids(self):
        self.assertTrue(has_invoice_structure(invoice_text()))
        self.assertEqual(classify(invoice_text()), '电子发票')

    def test_missing_title_uses_tax_identity_and_lowercase_total(self):
        text = '名称：某大学\n纳税人识别号12100000485001086E\n(小写)￥65.11'
        self.assertEqual(classify(text), '电子发票')

    def test_payment_invoice_link_is_not_enough(self):
        for text in ('订单详情\n查看电子发票\n发票号码12345678901234567890\n实付款35元',
                     '支付成功\n交易单号42000001234567890123456789\n申请电子发票',
                     '订单详情\n电子 发\n购买方12100000485001086E\n实付款35元',
                     '订单详情\n查看电子发票\n购买方12100000485001086E\n商家91340111MA00000001\n实付款35元'):
            with self.subTest(text=text):
                self.assertEqual(classify(text), '购买记录')

    def test_standalone_declaration_precedes_invoice_structures(self):
        self.assertEqual(classify('承诺书\n' + invoice_text() + '\n小写￥31.22'), '承诺书')

    def test_invoice_gibberish_from_stars_does_not_become_an_item(self):
        m = recognize(Material('random.png', '', '.png', text='电子发票\n*位*HhRH/ERH\n小写￥31.22'))
        self.assertFalse(m.item)
        self.assertTrue(any('物品名称' in issue for issue in m.issues))
        for item in ('USB3.0', 'U盘'):
            m = recognize(Material('random.png', '', '.png', text=f'电子发票\n*电子产品*{item}\n小写￥31.22'))
            self.assertEqual(m.item, item)

    def test_local_gross_requires_currency_and_explicit_decimal(self):
        self.assertEqual(local_gross('(小写) ¥31. 22'), ('31.22', []))
        for text in ('¥929', '31.22', '¥12.345', '¥1.20%', '- ¥1.20', '¥0.00', '¥31.22 ¥40'):
            with self.subTest(text=text):
                self.assertFalse(local_gross(text)[0])

    def test_comma_decimal_always_requires_review(self):
        value, warnings = local_gross('(小写)¥480, 00')
        self.assertEqual(value, '480.00')
        self.assertTrue(warnings)
        self.assertFalse(local_gross('¥31.22 ¥30.91')[0])


class InvoiceRegionTests(unittest.TestCase):
    def grid(self):
        im = Image.new('RGB', (600, 400), 'white')
        draw = ImageDraw.Draw(im)
        for y in (80, 145, 270, 295, 355):
            draw.line((5, y, 595, y), fill=(180, 30, 30), width=1)
        return im

    def test_regions_use_detected_rules_and_ignore_empty_image(self):
        im = self.grid()
        self.assertEqual(invoice_regions(im)['amount'], (360, 272, 588, 294))
        self.assertFalse(invoice_regions(Image.new('RGB', im.size, 'white')))
        im.close()

    def test_transparent_png_is_composited_before_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random.png'
            Image.new('RGBA', (50, 50), (0, 0, 0, 0)).save(path)
            pages = image_pages(path, Settings())
            im = next(pages)
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(im.getpixel((0, 0)), (255, 255, 255))
            im.close()
            pages.close()

    def test_locally_recognized_amount_has_coordinates_and_confirmation_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random.png'
            self.grid().save(path)
            m = recognize(Material(path.name, '', '.png', text=invoice_text()))
            block = dict(text='¥31.22', confidence=92, page=1, engine='test', box=[.1, .1, .9, .9])
            with patch('imss.recognition.extract.ocr_image', return_value=Extraction('¥31.22', [], [block])):
                refine_missing_amount(path, m, Settings())
            self.assertEqual(m.amount, '31.22')
            self.assertTrue(any('局部OCR' in issue for issue in m.issues))
            region = m.field_evidence['amount']['regions'][0]
            self.assertGreater(region['box'][0], .6)
            self.assertGreater(region['box'][1], .65)

    def test_missing_decimal_retry_never_guesses_or_overwrites_existing_amount(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'random.png'
            self.grid().save(path)
            m = recognize(Material(path.name, '', '.png', text=invoice_text()))
            with patch('imss.recognition.extract.ocr_image', return_value=Extraction('¥3122', [], [])) as ocr:
                refine_missing_amount(path, m, Settings())
            self.assertFalse(m.amount)
            self.assertEqual(ocr.call_count, 2)
            m.amount = '35.00'
            with patch('imss.recognition.extract.ocr_image') as ocr:
                refine_missing_amount(path, m, Settings())
            ocr.assert_not_called()
