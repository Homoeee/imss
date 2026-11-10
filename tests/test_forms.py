"""Configured person fields must not overwrite the template's promise image."""
import csv
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from docx import Document
import pypdfium2 as pdfium
from imss.details import load_details
from imss.output.documents import build_document
from imss.output.pdf import build_pdf
from imss.core.models import Bundle, Material, Settings
from imss.workflow import build, scan


class FormFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.signature = self.root / 'signature.png'
        Image.new('RGB', (400, 100), 'black').save(self.signature)
        self.materials = []
        for index, kind in enumerate(('电子发票', '购买记录')):
            path = self.root / f'material{index}.png'
            Image.new('RGB', (120, 100), ('white', 'gray')[index]).save(path)
            self.materials.append(Material(path.name, str(index), '.png', kind=kind, item='打印纸',
                                           amount='35.80', archived=str(path)))

    def tearDown(self):
        self.temporary.cleanup()

    def test_signature_appears_in_buyer_cell_and_promise_bytes_remain(self):
        destination = self.root / 'form.docx'
        build_document(Bundle('test', '打印纸', '35.80', self.materials), '02', None,
                       destination, Settings(), details={'signature_image': str(self.signature),
                                                        'student_id': 'TEST-ID', 'phone': 'TEST-PHONE'})
        document = Document(destination)
        self.assertFalse(document.sections[0]._sectPr.xpath('w:headerReference | w:footerReference'))
        declaration = document.tables[-1]
        buyer = declaration.cell(0, 0)
        buyer_ids = buyer._tc.xpath('.//a:blip/@r:embed')
        self.assertEqual(len(buyer_ids), 1)
        self.assertEqual(document.part.related_parts[buyer_ids[0]].blob, self.signature.read_bytes())
        promise_ids = declaration.cell(1, 3)._tc.xpath('.//a:blip/@r:embed')
        self.assertEqual(len(promise_ids), 1)
        promise = Path(__file__).resolve().parents[1] / 'imss/resources/templates/承诺不重复报销.png'
        self.assertEqual(document.part.related_parts[promise_ids[0]].blob, promise.read_bytes())
        text = '\n'.join(c.text for r in declaration.rows for c in r.cells)
        self.assertIn('TEST-ID', text)
        self.assertIn('TEST-PHONE', text)
        self.assertIn('☑否', text)
        self.assertIn('☑是', text)
        self.assertIn('□是，签领单附后', text)
        self.assertNotIn('{{', text)

    def test_prize_flag_from_details_and_cli_and_amount_agreement(self):
        from main import main

        manifest = self.make_scan()
        details = manifest.parent / 'detail.json'
        values = json.loads(details.read_text(encoding='utf-8'))
        self.assertEqual(values['物品']['is_prize'], 0)
        values['物品'].update(is_prize=1, amount_equal=False)
        details.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')

        for arguments, prize in (([], '☑是，签领单附后'), (['--is-prize', '0'], '☑否')):
            with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(main(['build', str(manifest), '--skip-pdf', *arguments]), 0)
                report = json.loads(manifest.read_text(encoding='utf-8'))['report']
                document = Document(Path(report['output']) / report['generated'][0]['word'])
                declaration = document.tables[-1]
                self.assertIn(prize, [cell.text for cell in declaration.rows[2].cells])
                self.assertIn('☑是', [cell.text for cell in declaration.rows[3].cells])

        for invalid in (2, '1', 1.0):
            with self.subTest(invalid=invalid):
                values['物品']['is_prize'] = invalid
                details.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'is_prize 必须是 0 或 1'):
                    load_details(details, {})

    def test_pdf_checkbox_marks_and_signature_to_right_of_label(self):
        pdf_path = build_pdf(Bundle('test', '打印纸', '35.80', self.materials), '01',
                             self.root / 'signed.pdf', Settings(), details={'signature_image': str(self.signature)})
        with pdfium.PdfDocument(pdf_path) as pdf, closing(pdf[0]) as page, closing(page.get_textpage()) as text:
            self.assertEqual(len(pdf), 1)
            self.assertIn('☑否', text.get_text_range())
            self.assertIn('☑是', text.get_text_range())
            with closing(text.search('签名：')) as search:
                start, length = search.get_next()
            _, label_bottom, label_right, label_top = text.get_charbox(start + length - 1)
            signatures = [image.get_bounds() for image in page.get_objects(filter=[3])]
            self.assertTrue(any(left >= label_right and bottom < label_top and top > label_bottom
                                and 10 < right - left < 70 for left, bottom, right, top in signatures))

    def test_custom_header_and_footer_fields_are_filled_without_adding_empty_parts(self):
        template = self.root / 'custom.docx'
        document = Document()
        section = document.sections[0]
        section.header.paragraphs[0].text = '{{item_name}}'
        section.first_page_footer.paragraphs[0].text = '{{amount}}'
        document.add_section()
        document.save(template)

        destination = self.root / 'form.docx'
        build_document(Bundle('test', '打印纸', '35.80', self.materials), '01', template, destination, Settings())
        first, second = Document(destination).sections
        self.assertEqual(first.header.paragraphs[0].text, '打印纸')
        self.assertEqual(first.first_page_footer.paragraphs[0].text, '35.80')
        self.assertEqual(len(first._sectPr.xpath('w:headerReference | w:footerReference')), 2)
        self.assertFalse(second._sectPr.xpath('w:headerReference | w:footerReference'))

    def test_travel_names_are_separate_from_signature_and_promise(self):
        for material in self.materials:
            material.category = '行程'
        destination = self.root / 'travel.docx'
        build_document(Bundle('test', '打车费用', '35.80', self.materials), '01', None,
                       destination, Settings(), details={'passenger_name': '张三',
                                                        'passenger_name_2': '李四',
                                                        'passenger_name_3': '王五',
                                                        'student_id': 'TEST-ID',
                                                        'signature_image': str(self.signature)})
        document = Document(destination)
        declaration = document.tables[-1]
        for index, name in enumerate(('张三', '李四', '王五')):
            self.assertIn(f'乘车人{index + 1}姓名：{name}', declaration.cell(index, 0).text)
            self.assertIn('签名：', declaration.cell(index, 0).text)
        self.assertIn('TEST-ID', declaration.cell(0, 3).text)
        signature_id = declaration.cell(0, 0)._tc.xpath('.//a:blip/@r:embed')[0]
        self.assertEqual(document.part.related_parts[signature_id].blob, self.signature.read_bytes())
        promise_id = declaration.cell(4, 3)._tc.xpath('.//a:blip/@r:embed')[0]
        promise = Path(__file__).resolve().parents[1] / 'imss/resources/templates/承诺不重复报销.png'
        self.assertEqual(document.part.related_parts[promise_id].blob, promise.read_bytes())
        self.assertNotIn('{{', '\n'.join(c.text for r in declaration.rows for c in r.cells))

    def test_travel_names_can_be_left_blank_without_signature(self):
        destination = self.root / 'travel.docx'
        build_document(Bundle('test', '打车费用', '35.80', self.materials), '01', None,
                       destination, Settings(), details={'category': '行程'})
        declaration = Document(destination).tables[-1]
        for index in range(3):
            cell = declaration.cell(index, 0)
            self.assertIn(f'乘车人{index + 1}姓名：', cell.text)
            self.assertNotIn('{{', cell.text)
            self.assertFalse(cell._tc.xpath('.//a:blip'))

    def test_travel_pdf_keeps_signed_form_and_overflow_attachments(self):
        materials = [self.materials[0]]
        for index in range(6):
            path = self.root / f'order{index}.png'
            Image.new('RGB', (120, 600), (index, 0, 0)).save(path)
            materials.append(Material(path.name, str(index), '.png', kind='购买记录', archived=str(path)))
        details = {'category': '行程', 'purpose': '现场布置往返', 'passenger_name': '张三',
                   'passenger_name_2': '李四', 'passenger_name_3': '王五'}
        for key in ('signature_image', 'signature_image_2', 'signature_image_3'):
            details[key] = str(self.signature)
        pdf_path = build_pdf(Bundle('test', '打车费用', '35.80', materials), '01',
                             self.root / 'travel.pdf', Settings(), details=details)
        with pdfium.PdfDocument(pdf_path) as pdf:
            self.assertEqual(len(pdf), 2)
            with closing(pdf[0]) as first, closing(first.get_textpage()) as text:
                self.assertIn('乘车人3姓名：王五', text.get_text_range())
                self.assertIn('现场布置往返', text.get_text_range())
            with closing(pdf[0]) as first, closing(pdf[1]) as second:
                self.assertEqual(sum(len(list(page.get_objects(filter=[3]))) for page in (first, second)), 11)

    def make_scan(self, travel=False):
        source = self.root / 'input'
        source.mkdir()
        for index in range(3 if travel else 2):
            Image.new('RGB', (120 + index, 100), 'white').save(source / f'{index}.png')
        texts = {'0.png': '电子发票\n物品名称：打印纸\n（小写）￥35.80',
                 '1.png': '订单编号：123456\n物品名称：打印纸\n实付款：35.80'}
        if travel:
            texts = {'0.png': '电子发票\n发票号码：12345678901234567890\n物品名称：打车费用\n（小写）￥35.80',
                     '1.png': '订单编号：123456\n物品名称：打车费用\n实付款：35.80',
                     '2.png': '高德地图打车行程单\n合计35.80元'}
        with patch('imss.workflow.extract', side_effect=lambda path, ext, settings: (texts[path.name], [])):
            return scan(source, None, Settings(workers=1))

    def test_group_person_fields_override_main_defaults_and_resolve_relative_images(self):
        manifest = self.make_scan()
        review = manifest.parent / '核对清单.xlsx'
        details = self.root / 'details.json'
        details.write_text(json.dumps({'0.png': {'phone': 'GROUP-PHONE', 'signature_image': 'signature.png'}}), encoding='utf-8')
        from main import main
        with redirect_stdout(io.StringIO()):
            status = main(['build', str(manifest), '--review', str(review), '--details', str(details),
                           '--skip-pdf', '--phone', 'CLI-PHONE'],
                          person_defaults={'student_id': 'DEFAULT-ID', 'phone': 'DEFAULT-PHONE'})
        self.assertEqual(status, 0)
        report = json.loads(manifest.read_text(encoding='utf-8'))['report']
        self.assertFalse(report['pending'])
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        text = '\n'.join(c.text for t in document.tables for r in t.rows for c in r.cells)
        self.assertIn('DEFAULT-ID', text)
        self.assertIn('GROUP-PHONE', text)
        self.assertNotIn('DEFAULT-PHONE', text)
        self.assertNotIn('CLI-PHONE', text)
        self.assertEqual(len(document.tables[-1].cell(0, 0)._tc.xpath('.//a:blip')), 1)

    def test_invalid_signature_is_rejected_before_archiving(self):
        manifest = self.make_scan()
        with self.assertRaisesRegex(ValueError, '签字图片不存在'):
            build(manifest, skip_pdf=True, person_defaults={'signature_image': str(self.root / 'missing.png')})
        self.assertFalse(any((self.root / '报销材料').iterdir()))

    def test_detail_json_accepts_raw_and_escaped_windows_paths(self):
        details = self.root / 'detail.json'
        for spelling, expected in (
            (r'D:\照片\template.docx', r'D:\照片\template.docx'),
            (r'D:\new\temp.docx', r'D:\new\temp.docx'),
            (r'D:\\照片\\template.docx', r'D:\照片\template.docx'),
            (r'D:\\\u7167\u7247\\template.docx', r'D:\照片\template.docx'),
            ('D:/照片/template.docx', 'D:/照片/template.docx'),
            (r'\\server\share\template.docx', r'\\server\share\template.docx'),
        ):
            with self.subTest(spelling=spelling):
                details.write_text('{"物品":{"template":"' + spelling + r'","purpose":"第一行\n第二行"}}',
                                   encoding='utf-8-sig')
                self.assertEqual(load_details(details, {})['物品'],
                                 {'template': expected, 'purpose': '第一行\n第二行'})
        details.write_text(r'{"物品":{"purpose":"保留错误\路径"}}', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            load_details(details, {})

    def test_detail_json_accepts_raw_windows_paths_in_signature_fields(self):
        details = self.root / 'detail.json'
        details.write_text(r'{"行程":{"signature_image":"签名\承诺.png",'
                           r'"passengers":[{"signature_image":"new\test.png"}]}}', encoding='utf-8')
        with patch('imss.details.validate_signature') as validate:
            load_details(details, {})
        self.assertEqual([call.args[0] for call in validate.call_args_list],
                         [self.root / r'签名\承诺.png', self.root / r'new\test.png'])

    def test_visible_auto_group_details_and_invoice_override(self):
        manifest = self.make_scan(travel=True)
        data = json.loads(manifest.read_text(encoding='utf-8'))
        invoice = next(m for m in data['materials'] if m['kind'] == '电子发票')
        group = 'auto-' + invoice['digest'][:16]
        self.assertTrue(group.startswith('auto-'))
        details = self.root / 'detail.json'
        fields = {group: {'passenger_name': '分组姓名', 'passenger_name_2': '同行人',
                          'purpose': '现场布置往返'}}
        details.write_text(json.dumps(fields, ensure_ascii=False), encoding='utf-8-sig')
        report = build(manifest, skip_pdf=True, details_path=details,
                       person_defaults={'passenger_name': '默认姓名'})
        self.assertFalse(report['pending'])
        def text_in_report(report):
            document = Document(Path(report['output']) / report['generated'][0]['word'])
            return '\n'.join(c.text for t in document.tables for r in t.rows for c in r.cells)
        text = text_in_report(report)
        self.assertIn('乘车人1姓名：分组姓名', text)
        self.assertIn('乘车人2姓名：同行人', text)
        self.assertIn('现场布置往返', text)
        self.assertNotIn('默认姓名', text)
        fields['12345678901234567890'] = {'passenger_name': '发票姓名'}
        details.write_text(json.dumps(fields, ensure_ascii=False), encoding='utf-8')
        report = build(manifest, skip_pdf=True, details_path=details,
                       person_defaults={'passenger_name': '默认姓名'})
        self.assertFalse(report['pending'])
        text = text_in_report(report)
        self.assertIn('乘车人1姓名：发票姓名', text)
        self.assertIn('乘车人2姓名：同行人', text)
        self.assertNotIn('分组姓名', text)

    def test_cli_default_passenger_name(self):
        manifest = self.make_scan(travel=True)
        from main import main
        with redirect_stdout(io.StringIO()):
            status = main(['build', str(manifest), '--passenger-name', '张三', '--skip-pdf'])
        self.assertEqual(status, 0)
        report = json.loads(manifest.read_text(encoding='utf-8'))['report']
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        self.assertIn('乘车人1姓名：张三', document.tables[-1].cell(0, 0).text)

    def test_passenger_names_must_be_strings_before_archiving(self):
        manifest = self.make_scan()
        for field in ('passenger_name', 'passenger_name_2', 'passenger_name_3'):
            with self.subTest(field=field):
                details = self.root / 'detail.json'
                details.write_text(json.dumps({'test': {field: ['张三']}}), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, field + ' 必须是字符串'):
                    build(manifest, skip_pdf=True, details_path=details)
                self.assertFalse(any((self.root / '报销材料').iterdir()))

    def test_category_file_generated_and_automatically_reloaded(self):
        manifest = self.make_scan()
        path = manifest.parent / 'detail.json'
        fields = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(set(fields), {'物品', '奖品', '工作餐', '行程', '快递'})
        self.assertEqual(fields['物品']['signature_image'], '')
        fields['物品'].update(student_id='CATEGORY-ID', phone='CATEGORY-PHONE',
                             signature_image='../signature.png')
        fields['行程']['student_id'] = 'UNRELATED-ID'
        path.write_text(json.dumps(fields), encoding='utf-8')
        for phone in ('CATEGORY-PHONE', 'UPDATED-PHONE'):
            fields['物品']['phone'] = phone
            path.write_text(json.dumps(fields), encoding='utf-8')
            report = build(manifest, skip_pdf=True)
            self.assertEqual(report['details_file'], str(path.resolve()))
            document = Document(Path(report['output']) / report['generated'][0]['word'])
            text = '\n'.join(p.text for t in document.tables for r in t.rows for c in r.cells for p in c.paragraphs)
            self.assertIn('CATEGORY-ID', text)
            self.assertIn(phone, text)
            self.assertNotIn('UNRELATED-ID', text)
            self.assertTrue(document.tables[-1].cell(0, 0)._tc.xpath('.//a:blip'))
        from imss.workflow import update_review, recheck
        before = path.read_bytes()
        update_review(manifest)
        with patch('imss.workflow.extract', return_value=('电子发票', [])):
            recheck(manifest)
        self.assertEqual(path.read_bytes(), before)

    def test_multiple_passengers_have_individual_signatures_and_ids(self):
        manifest = self.make_scan(travel=True)
        fields = {'行程': {'passengers': [
            {'name': name, 'student_id': f'ID-{index}', 'signature_image': '../signature.png'}
            for index, name in enumerate(('张三', '李四', '王五'))]}}
        path = manifest.parent / 'detail.json'
        path.write_text(json.dumps(fields), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        self.assertFalse(report['pending'])
        declaration = Document(Path(report['output']) / report['generated'][0]['word']).tables[-1]
        for index, name in enumerate(('张三', '李四', '王五')):
            self.assertIn(name, declaration.cell(index, 0).text)
            self.assertIn(f'ID-{index}', declaration.cell(index, 3).text)
            self.assertEqual(len(declaration.cell(index, 0)._tc.xpath('.//a:blip')), 1)
        # 更新后少一人，旧的第三位信息不会残留。
        fields['行程']['passengers'].pop()
        path.write_text(json.dumps(fields), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        declaration = Document(Path(report['output']) / report['generated'][0]['word']).tables[-1]
        self.assertNotIn('王五', declaration.cell(2, 0).text)
        self.assertFalse(declaration.cell(2, 0)._tc.xpath('.//a:blip'))
        self.assertNotIn('ID-2', declaration.cell(2, 3).text)

    def test_travel_purpose_is_loaded_and_original_promise_is_preserved(self):
        manifest = self.make_scan(travel=True)
        path = manifest.parent / 'detail.json'
        fields = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(fields['行程']['purpose'], '')
        self.assertNotIn('promise_image', fields['行程'])
        promise = Path(__file__).resolve().parents[1] / 'imss/resources/templates/承诺不重复报销.png'
        fields['行程'].update(purpose='活动负责人前往高新校区参与活动筹备往返',
                             signature_image='../signature.png')
        path.write_text(json.dumps(fields), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        self.assertFalse(report['pending'])
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        declaration = document.tables[-1]
        self.assertIn(fields['行程']['purpose'], declaration.cell(3, 1).text)
        promise_ids = declaration.cell(4, 3)._tc.xpath('.//a:blip/@r:embed')
        self.assertEqual(len(promise_ids), 1)
        self.assertEqual(document.part.related_parts[promise_ids[0]].blob, promise.read_bytes())
        person_id = declaration.cell(0, 0)._tc.xpath('.//a:blip/@r:embed')[0]
        self.assertEqual(document.part.related_parts[person_id].blob, self.signature.read_bytes())
        fields['行程']['purpose'] = ''
        path.write_text(json.dumps(fields), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        declaration = document.tables[-1]
        self.assertEqual(declaration.cell(3, 1).text, '')
        promise_id = declaration.cell(4, 3)._tc.xpath('.//a:blip/@r:embed')[0]
        original = Path(__file__).resolve().parents[1] / 'imss/resources/templates/承诺不重复报销.png'
        self.assertEqual(document.part.related_parts[promise_id].blob, original.read_bytes())

    def test_invalid_travel_purpose_fails_before_publishing(self):
        manifest = self.make_scan(travel=True)
        path = manifest.parent / 'detail.json'
        for fields in ({'purpose': 123}, {'purpose': []}):
            with self.subTest(fields=fields):
                path.write_text(json.dumps({'行程': fields}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    build(manifest, skip_pdf=True)
                self.assertFalse(any((self.root / '报销材料').iterdir()))

    def test_category_does_not_fill_other_categories_and_invoice_wins(self):
        manifest = self.make_scan()
        path = manifest.parent / 'detail.json'
        path.write_text(json.dumps({'行程': {'student_id': 'TRAVEL-ID'}}), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        self.assertNotIn('TRAVEL-ID', document.tables[-1].cell(0, 2).text)
        self.assertFalse(document.tables[-1].cell(0, 0)._tc.xpath('.//a:blip'))
        path.write_text(json.dumps({'物品': {'student_id': 'CATEGORY-ID'},
                                    '0.png': {'student_id': 'INVOICE-ID'}}), encoding='utf-8')
        report = build(manifest, skip_pdf=True)
        document = Document(Path(report['output']) / report['generated'][0]['word'])
        self.assertIn('INVOICE-ID', document.tables[-1].cell(0, 2).text)
        self.assertNotIn('CATEGORY-ID', document.tables[-1].cell(0, 2).text)

    def test_invalid_passenger_list_fails_before_publishing(self):
        manifest = self.make_scan()
        path = manifest.parent / 'detail.json'
        for passengers in ('张三', [{}] * 4, ['张三'], [{'student_id': 123}],
                           [{'signature_image': 'missing.png'}]):
            with self.subTest(passengers=passengers):
                path.write_text(json.dumps({'行程': {'passengers': passengers}}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    build(manifest, skip_pdf=True)
                self.assertFalse(any((self.root / '报销材料').iterdir()))
