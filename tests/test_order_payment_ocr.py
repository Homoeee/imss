"""订单金额补读回归：付款标签乱码、单价/返现干扰和冲突保护。"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from imss.core.models import Material, Settings
from imss.recognition.extract import Extraction
from imss.recognition.refine import refine_missing_amount
from imss.recognition.rules import recognize


def block(text, y=.8, page=1):
    return dict(text=text, box=[.55, y, .96, y + .03], confidence=90,
                page=page, engine='tesseract')


def order():
    return recognize(Material('anonymous.png', 'sample', '.png',
        text='交易成功\n金属拆机维修工具 ¥4.04\nx4\n返1元支付宝现金\n10.21 Sex ¥16.18',
        layout=[block('10.21 Sex ¥16.18')]))


class OrderPaymentOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'anonymous.png'
        Image.new('RGB', (400, 600), 'white').save(self.path)

    def retry(self, material, text, issues=None):
        result = Extraction(text, issues or [], [block(line, .1 + index * .05)
                                                for index, line in enumerate(text.splitlines())])
        with patch('imss.recognition.refine.ocr_image', return_value=result) as ocr:
            refine_missing_amount(self.path, material, Settings())
        return ocr

    def test_recovers_paid_amount_and_preserves_first_pass_fields(self):
        material = order()
        before = deepcopy(material)
        self.assertEqual(material.amount, '')
        ocr = self.retry(material, '交易成功\n乱码商品 ¥4.04\nx4\n返1元支付宝现金\n10.21\n实 付 款 ¥16.18')
        self.assertEqual(material.amount, '16.18')
        self.assertEqual((material.item, material.kind, material.text),
                         (before.item, before.kind, before.text))
        self.assertEqual(material.field_evidence['item'], before.field_evidence['item'])
        self.assertFalse(material.issues)
        self.assertIn('16.18', material.related_amounts)
        entry = material.field_evidence['amount']
        self.assertEqual(entry['value'], '16.18')
        self.assertTrue(any('16.18' in region['text'] for region in entry['regions']))
        self.assertTrue(all('乱码商品' not in b['text'] for b in material.layout))
        self.assertEqual(ocr.call_args.kwargs, {'_psm': 11})

    def test_rejects_unlabelled_price_cashback_and_unconfirmed_total(self):
        for text in ('交易成功\n商品 ¥4.04\nx4\n返1元支付宝现金\n¥16.18',
                     '交易成功\n订单总额 ¥16.18',
                     '交易成功\n实付款\n运费 ¥4.04',
                     '交易成功\n实付款 ¥1618 18',
                     '交易成功\n实付款 ¥16.18\n实际支付 ¥4.04',
                     '交易成功\n实付款 ¥16.18\n已退款'):
            with self.subTest(text=text):
                material = order()
                self.retry(material, text)
                self.assertEqual(material.amount, '')

    def test_existing_amount_and_explicit_conflicts_are_not_retried(self):
        for text in ('交易成功\n商品名称：维修工具\n实付款 ¥16.18',
                     '交易成功\n实付款 ¥16.18\n实际支付 ¥4.04',
                     '交易成功\n实付款 ???\n已退款',
                     '订单编号123456789\n待付款'):
            with self.subTest(text=text):
                material = recognize(Material('a.png', 'sample', '.png', text=text,
                                              layout=[block('¥16.18')]))
                before = deepcopy(material)
                with patch('imss.recognition.refine.ocr_image') as ocr:
                    refine_missing_amount(Path('not-opened.png'), material, Settings())
                ocr.assert_not_called()
                self.assertEqual(material, before)

    def test_multi_page_evidence_and_multi_frame_images_are_not_retried(self):
        material = order()
        material.layout.append(block('¥4.04', page=2))
        with patch('imss.recognition.refine.ocr_image') as ocr:
            refine_missing_amount(self.path, material, Settings())
        ocr.assert_not_called()
        frames = [Image.new('RGB', (400, 600), color) for color in ('white', 'gray')]
        self.addCleanup(lambda: [frame.close() for frame in frames])
        path = self.path.with_suffix('.tiff')
        frames[0].save(path, save_all=True, append_images=frames[1:])
        material = order()
        material.extension = '.tiff'
        with patch('imss.recognition.refine.ocr_image') as ocr:
            refine_missing_amount(path, material, Settings())
        ocr.assert_not_called()

    def test_retry_with_low_confidence_leaves_amount_blank(self):
        material = order()
        self.retry(material, '交易成功\n实付款 ¥16.18', ['OCR 平均置信度偏低，请核对原件'])
        self.assertEqual(material.amount, '')


if __name__ == '__main__':
    unittest.main()
