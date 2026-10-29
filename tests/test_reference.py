"""Private-corpus regression checks; clean GitHub checkouts skip without fixtures."""
import json
from pathlib import Path
import unittest

import pypdfium2 as pdfium

from imss.recognition.extract import extract_pdf
from imss.storage import digest_file
from imss.core.models import Material, Settings
from imss.recognition.rules import recognize

CORPUS = Path(__file__).parent / 'fixtures' / 'reimbursement'


@unittest.skipUnless((CORPUS / 'oracle.json').is_file(), '本地私有测试集未提供；CI 不上传真实材料')
class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.oracle = json.loads((CORPUS / 'oracle.json').read_text(encoding='utf-8'))

    def test_shuffle_preserves_original_bytes(self):
        reference = Path(self.oracle['reference'])
        for record in self.oracle['files']:
            with self.subTest(file=record['file']):
                self.assertEqual(digest_file(CORPUS / 'input' / record['file']), record['sha256'])
                self.assertEqual(digest_file(reference / record['original']), record['sha256'])

    def test_all_text_pdf_invoice_totals_include_cents(self):
        for record in self.oracle['files']:
            if record['kind'] != '电子发票' or not record['file'].endswith('.pdf'):
                continue
            with self.subTest(file=record['file']):
                text, issues = extract_pdf(CORPUS / 'input' / record['file'], Settings())
                self.assertFalse(issues)
                material = Material(record['file'], record['sha256'], '.pdf', text=text)
                recognize(material)
                self.assertEqual(material.amount, record['invoice_amount'])

    def test_reference_documents_are_all_one_page(self):
        reference = Path(self.oracle['reference'])
        self.assertEqual(len(self.oracle['groups']), 44)
        for group in self.oracle['groups']:
            with self.subTest(group=group['id']), pdfium.PdfDocument(reference / group['reference_pdf']) as pdf:
                self.assertEqual(len(pdf), 1)


if __name__ == '__main__':
    unittest.main()
