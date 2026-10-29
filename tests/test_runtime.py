"""Portable bundles must work without PATH or a particular working directory."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from imss.recognition.extract import ocr_image, tesseract_path
from imss.core.models import Settings
from imss.runtime.office import convert_office, soffice_path
from imss.runtime.paths import bundled_soffice, bundled_tesseract, tessdata_path


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.patch = patch('imss.runtime.paths.VENDOR_ROOT', self.root / 'vendor')
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def test_only_project_bundle_is_used_even_when_path_has_programs(self):
        for candidate, resolve in ((bundled_tesseract()[0], tesseract_path), (bundled_soffice()[0], soffice_path)):
            path = Path(candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
            with patch('imss.runtime.office.shutil.which', return_value='system-program') as search:
                self.assertEqual(resolve(Settings()), str(path.resolve()))
                settings = Settings(tesseract=str(path), soffice=str(path))
                self.assertEqual(resolve(settings), str(path.resolve()))
                search.assert_not_called()

    def test_project_models_are_used_by_ocr_without_configuration(self):
        models = self.root / 'vendor/tesseract/tessdata'
        models.mkdir(parents=True)
        self.assertEqual(tessdata_path(Settings()), models.resolve())
        explicit = self.root / 'vendor/custom-models'
        explicit.mkdir()
        self.assertEqual(tessdata_path(Settings(tessdata=str(explicit))), explicit.resolve())
        tsv = b'text\tconf\tline_num\tpage_num\tblock_num\tpar_num\n35.80\t99\t1\t1\t1\t1\n'
        with patch('imss.recognition.extract.tesseract_path', return_value='tesseract'), patch(
                'imss.recognition.extract.run_process', return_value=subprocess.CompletedProcess([], 0, tsv, b'')) as process:
            text, issues = ocr_image(Image.new('RGB', (100, 100)), Settings())
        self.assertEqual(text, '35.80')
        self.assertFalse(issues)
        arguments = process.call_args.args[0]
        self.assertEqual(arguments[arguments.index('--tessdata-dir') + 1], str(models.resolve()))

    def test_missing_bundle_never_falls_back_to_installed_programs(self):
        with self.assertRaisesRegex(RuntimeError, '模型目录缺失'):
            tessdata_path(Settings())
        with patch('imss.runtime.office.shutil.which', return_value='installed-program') as search:
            for resolve in (tesseract_path, soffice_path):
                with self.assertRaisesRegex(RuntimeError, 'vendor'):
                    resolve(Settings())
            search.assert_not_called()

    def test_external_configured_paths_are_rejected(self):
        external = self.root / 'external-program'
        external.write_bytes(b'fixture')
        settings = Settings(tesseract=str(external), soffice=str(external), tessdata=str(self.root))
        for resolve in (tesseract_path, soffice_path, tessdata_path):
            with self.assertRaisesRegex(RuntimeError, 'vendor'):
                resolve(settings)

    def test_relative_component_paths_are_based_on_project_directory(self):
        executable = Path(bundled_tesseract()[0])
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture')
        with patch('imss.runtime.paths.PROJECT_ROOT', self.root):
            relative = executable.relative_to(self.root)
            self.assertEqual(tesseract_path(Settings(tesseract=str(relative))), str(executable.resolve()))

    def test_word_backend_is_rejected_including_legacy_settings(self):
        with self.assertRaises(ValueError):
            Settings.from_dict({'office_backend': 'word'})
        with self.assertRaisesRegex(RuntimeError, '项目内 LibreOffice'):
            convert_office(Path('input.docx'), Path('output.pdf'), Settings(office_backend='word'))
