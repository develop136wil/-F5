"""Offline OCR unit checks: generated images, mock inference, no model download."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from services import local_ocr as ocr


def png_bytes(width=2, height=2):
    """Small RGBA PNG; oversized dimensions are only headers, never allocated."""
    def chunk(kind, payload):
        return (struct.pack('>I', len(payload)) + kind + payload
                + struct.pack('>I', zlib.crc32(kind + payload)))
    pixels = (b'\0' + b'\xff\0\0\xff' * 2) * 2
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class TestLocalOcr(unittest.TestCase):
    def infer(self, texts, scores, **kwargs):
        engine = Mock(return_value=SimpleNamespace(txts=texts, scores=scores))
        with patch.object(ocr, '_engine', return_value=engine) as factory:
            result = ocr.extract_image_document(png_bytes(), **kwargs)
        factory.assert_called_once_with()
        engine.assert_called_once()
        self.last_image = engine.call_args.args[0]
        return result

    def test_high_confidence_still_requires_original_verification(self):
        result = self.infer(['지원 대상\n아동', '접수 2026. 9. 30.'], [0.999, 1.0])
        self.assertEqual(result.text, '지원 대상 아동 접수 2026. 9. 30.')
        self.assertEqual(result.reasons, ['ocr_verification_required'])
        self.assertEqual((result.units_total, result.units_examined), (1, 1))
        self.assertEqual(self.last_image.shape, (2, 2, 3))
        self.assertEqual(self.last_image[0, 0].tolist(), [0, 0, 255])

    def test_invalid_bytes_urls_paths_and_empty_input_never_construct_engine(self):
        for value in (b'', b'not an image', b'https://example.org/image.png',
                      'https://example.org/image.png', 'C:/private/photo.png',
                      None, bytearray(png_bytes())):
            with self.subTest(type=type(value).__name__), patch.object(ocr, '_engine') as factory:
                result = ocr.extract_image_document(value)
            factory.assert_not_called()
            self.assertEqual((result.text, result.reasons), ('', ['image_read_error']))
            self.assertEqual(result.units_examined, 0)

    def test_input_byte_limit_never_constructs_engine(self):
        with patch.object(ocr, '_engine') as factory:
            result = ocr.extract_image_document(b'x' * (20 * 1024 * 1024 + 1))
        factory.assert_not_called()
        self.assertEqual(result.reasons, ['image_read_error'])

    def test_pixel_and_side_limits_are_checked_before_image_decode_or_engine(self):
        for width, height in ((6001, 1), (1, 6001), (4500, 4500), (6000, 6000)):
            with self.subTest(size=(width, height)), patch.object(ocr, '_engine') as factory:
                result = ocr.extract_image_document(png_bytes(width, height))
            factory.assert_not_called()
            self.assertEqual((result.text, result.reasons), ('', ['image_size_limit']))

    def test_unsupported_image_format_is_rejected_before_engine(self):
        # Valid 1x1 GIF; OCR allowlist is PNG, JPEG, WEBP, not GIF.
        gif = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
               b',\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x01L\x00;')
        with patch.object(ocr, '_engine') as factory:
            result = ocr.extract_image_document(gif)
        factory.assert_not_called()
        self.assertEqual(result.reasons, ['image_read_error'])

    def test_output_is_bounded_by_max_chars_with_explicit_marker(self):
        result = self.infer(['가나다라마바사', '추가 출력 금지'], [0.99, 0.99], max_chars=5)
        self.assertEqual(result.text, '가나다라마')
        self.assertEqual(result.max_chars, 5)
        self.assertIn('char_limit', result.reasons)
        self.assertIn('ocr_verification_required', result.reasons)

    def test_character_budget_spans_lines_and_counts_separator(self):
        result = self.infer(['지원', '대상아동'], [0.99, 0.99], max_chars=5)
        self.assertEqual(result.text, '지원 대상')
        self.assertIn('char_limit', result.reasons)

    def test_invalid_character_budget_is_rejected(self):
        for value in (0, -1, True):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                ocr.extract_image_document(png_bytes(), max_chars=value)

    def test_medium_confidence_text_is_preserved_but_warned(self):
        result = self.infer(['확인 필요한 금액 50만원'], [0.6])
        self.assertEqual(result.text, '확인 필요한 금액 50만원')
        self.assertIn('ocr_low_confidence', result.reasons)
        self.assertIn('ocr_verification_required', result.reasons)
        self.assertNotIn('ocr_omitted_text', result.reasons)

    def test_below_half_confidence_is_omitted_without_hiding_good_lines(self):
        result = self.infer(['잘못 인식된 숫자 9999', '지원 안내'], [0.49, 0.98])
        self.assertEqual(result.text, '지원 안내')
        self.assertIn('ocr_low_confidence', result.reasons)
        self.assertIn('ocr_omitted_text', result.reasons)
        self.assertIn('ocr_verification_required', result.reasons)

    def test_invalid_or_missing_confidence_is_not_certified(self):
        for scores in ([float('nan')], [float('inf')], [-0.1], [1.1], [], None):
            with self.subTest(scores=scores):
                result = self.infer(['검증 안 된 문장'], scores)
            self.assertEqual(result.text, '')
            self.assertIn('ocr_omitted_text', result.reasons)
            self.assertIn('no_text_image', result.reasons)
            self.assertNotIn('ocr_verification_required', result.reasons)

    def test_half_confidence_boundary_preserves_text_with_warning(self):
        result = self.infer(['경계값 문장'], [0.5])
        self.assertEqual(result.text, '경계값 문장')
        self.assertIn('ocr_low_confidence', result.reasons)
        self.assertNotIn('ocr_omitted_text', result.reasons)

    def test_empty_and_whitespace_output_report_no_text(self):
        for texts, scores in (([], []), (None, None), (['  \n'], [0.99])):
            with self.subTest(texts=texts):
                result = self.infer(texts, scores)
            self.assertEqual(result.text, '')
            self.assertIn('no_text_image', result.reasons)
            self.assertNotIn('ocr_verification_required', result.reasons)

    def test_non_string_result_is_skipped_but_other_lines_survive(self):
        result = self.infer([None, '지원 내용'], [0.99, 0.99])
        self.assertEqual(result.text, '지원 내용')
        self.assertIn('ocr_read_error', result.reasons)

    def test_model_unavailable_error_never_leaks_raw_exception_text(self):
        secret = 'synthetic-private-name 010-0000-0000 api_key=not-a-real-key'
        for error in (ImportError(secret), ocr.LocalOcrUnavailable(secret), RuntimeError(secret)):
            with self.subTest(error=type(error).__name__), patch.object(ocr, '_engine', side_effect=error):
                result = ocr.extract_image_document(png_bytes())
            self.assertEqual((result.text, result.reasons), ('', ['ocr_models_unavailable']))
            self.assertNotIn(secret, repr(result))

    def test_missing_image_library_reports_unavailable_without_loading_engine(self):
        with patch.dict(sys.modules, {'PIL': None}), patch.object(ocr, '_engine') as factory:
            result = ocr.extract_image_document(png_bytes())
        factory.assert_not_called()
        self.assertEqual((result.text, result.reasons), ('', ['parser_unavailable']))

    def test_inference_error_never_leaks_raw_exception_text(self):
        secret = 'synthetic-private-name 010-0000-0000 token=not-a-real-token'
        engine = Mock(side_effect=RuntimeError(secret))
        with patch.object(ocr, '_engine', return_value=engine):
            result = ocr.extract_image_document(png_bytes())
        self.assertEqual(result.text, '')
        self.assertIn('ocr_read_error', result.reasons)
        self.assertIn('no_text_image', result.reasons)
        self.assertNotIn(secret, repr(result))


class TestOcrModelVerification(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ocr-manifest-test-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        entries = []
        for role in ('detector', 'orientation_classifier', 'korean_recognizer'):
            payload = ('synthetic-' + role).encode()
            filename = role + '.onnx'
            (self.folder / filename).write_bytes(payload)
            entries.append({'role': role, 'filename': filename, 'size_bytes': len(payload),
                            'sha256': hashlib.sha256(payload).hexdigest()})
        self.manifest = {'schema_version': 1, 'language': 'korean',
                         'runtime_downloads': False, 'models': entries}
        self.save(self.manifest)

    def save(self, manifest):
        (self.folder / 'model_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_verified_manifest_returns_only_local_resolved_models(self):
        models = ocr._verified_models(self.folder)
        self.assertEqual(set(models), {'detector', 'orientation_classifier', 'korean_recognizer'})
        for path in models.values():
            self.assertEqual(Path(path).parent, self.folder.resolve())
            self.assertTrue(Path(path).is_file())

    def test_path_traversal_absolute_paths_urls_and_alternate_streams_rejected(self):
        for name in ('../outside.onnx', '..\\outside.onnx', '/outside.onnx',
                     'C:\\outside.onnx', 'https://example.org/model.onnx',
                     'model.onnx:secret', 'folder/model.onnx'):
            with self.subTest(name=name):
                manifest = copy.deepcopy(self.manifest)
                manifest['models'][0]['filename'] = name
                self.save(manifest)
                with self.assertRaises(ocr.LocalOcrUnavailable):
                    ocr._verified_models(self.folder)

    def test_sha_mismatch_and_changed_model_size_are_rejected(self):
        for field, value in (('sha256', '0' * 64), ('size_bytes', 123456),
                             ('size_bytes', True), ('sha256', 'not-a-sha')):
            with self.subTest(field=field, value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest['models'][0][field] = value
                self.save(manifest)
                with self.assertRaises(ocr.LocalOcrUnavailable):
                    ocr._verified_models(self.folder)

    def test_manifest_schema_language_download_flag_and_roles_are_checked(self):
        variants = [dict(self.manifest, schema_version=2), dict(self.manifest, language='english'),
                    dict(self.manifest, runtime_downloads=True), dict(self.manifest, models=[])]
        duplicate = copy.deepcopy(self.manifest)
        duplicate['models'][1]['role'] = 'detector'
        variants.append(duplicate)
        for manifest in variants:
            with self.subTest(manifest=manifest):
                self.save(manifest)
                with self.assertRaises(ocr.LocalOcrUnavailable):
                    ocr._verified_models(self.folder)

    def test_missing_or_oversized_manifest_is_rejected(self):
        manifest_path = self.folder / 'model_manifest.json'
        manifest_path.unlink()
        with self.assertRaises(ocr.LocalOcrUnavailable):
            ocr._verified_models(self.folder)
        manifest_path.write_text(' ' * 16001, encoding='utf-8')
        with self.assertRaises(ocr.LocalOcrUnavailable):
            ocr._verified_models(self.folder)

    def test_runtime_download_hook_always_denies(self):
        with self.assertRaises(ocr.LocalOcrUnavailable):
            ocr._deny_download('https://example.org/model.onnx', 'ignored-local-path')

    def test_engine_disables_telemetry_and_downloads_and_uses_local_paths(self):
        runtime = ModuleType('onnxruntime')
        runtime.disable_telemetry_events = Mock()
        rapid = ModuleType('rapidocr')
        rapid.EngineType = SimpleNamespace(ONNXRUNTIME='onnx')
        rapid.LangDet, rapid.LangRec = SimpleNamespace(CH='ch'), SimpleNamespace(KOREAN='korean')
        rapid.ModelType = SimpleNamespace(MOBILE='mobile')
        rapid.OCRVersion = SimpleNamespace(PPOCRV4='v4', PPOCRV5='v5')
        rapid.RapidOCR = Mock(return_value=Mock())
        downloader = ModuleType('rapidocr.utils.download_file')
        original_download = Mock()
        downloader.DownloadFile = type('DownloadFile', (), {'run': staticmethod(original_download)})
        models = {role: str(self.folder / (role + '.onnx')) for role in
                  ('detector', 'orientation_classifier', 'korean_recognizer')}
        ocr._engine.cache_clear()
        self.addCleanup(ocr._engine.cache_clear)
        with patch.dict(sys.modules, {'onnxruntime': runtime, 'rapidocr': rapid,
                'rapidocr.utils': ModuleType('rapidocr.utils'),
                'rapidocr.utils.download_file': downloader}), patch.object(ocr, '_verified_models', return_value=models):
            first = ocr._engine()
            self.assertIs(first, ocr._engine())
            with self.assertRaises(ocr.LocalOcrUnavailable):
                downloader.DownloadFile.run('https://example.org/forbidden.onnx')
        original_download.assert_not_called()
        runtime.disable_telemetry_events.assert_called_once_with()
        rapid.RapidOCR.assert_called_once()
        params = rapid.RapidOCR.call_args.kwargs['params']
        self.assertEqual(params['Det.model_path'], models['detector'])
        self.assertEqual(params['Cls.model_path'], models['orientation_classifier'])
        self.assertEqual(params['Rec.model_path'], models['korean_recognizer'])
        self.assertFalse(params['Global.use_cls'])
        self.assertEqual(params['Global.text_score'], 0)
        self.assertFalse(params['EngineConfig.onnxruntime.use_cuda'])
        self.assertFalse(params['EngineConfig.onnxruntime.use_dml'])


if __name__ == '__main__':
    unittest.main()
