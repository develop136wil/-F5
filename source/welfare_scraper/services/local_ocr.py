"""Offline Korean OCR. Import is inert; inference belongs in a disposable worker."""
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re
import warnings

from services.document_text import DocumentText, TextBudget

MAX_PIXELS = 20000000
MAX_SIDE = 6000
MODEL_DIR = Path(__file__).resolve().parents[1] / 'assets' / 'local_ocr'


class LocalOcrUnavailable(RuntimeError):
    pass


def _deny_download(*_args, **_kwargs):
    raise LocalOcrUnavailable('Runtime OCR downloads are disabled')


def _verified_models(folder=MODEL_DIR):
    manifest_path = folder / 'model_manifest.json'
    if not manifest_path.is_file() or manifest_path.stat().st_size > 16000:
        raise LocalOcrUnavailable('OCR manifest unavailable')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entries = manifest.get('models')
    if (manifest.get('schema_version') != 1 or manifest.get('language') != 'korean'
            or manifest.get('runtime_downloads') is not False
            or not isinstance(entries, list) or len(entries) != 3):
        raise LocalOcrUnavailable('OCR manifest invalid')
    models = {}
    for entry in entries:
        name, digest = entry.get('filename'), entry.get('sha256')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+\.onnx', name)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise LocalOcrUnavailable('OCR model path invalid')
        path = folder / name
        size = entry.get('size_bytes')
        if (type(size) is not int or not 0 < size < 50 * 1024 * 1024
                or path.is_symlink() or not path.is_file() or path.stat().st_size != size
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise LocalOcrUnavailable('OCR model verification failed')
        models[entry.get('role')] = str(path.resolve())
    if set(models) != {'detector', 'orientation_classifier', 'korean_recognizer'}:
        raise LocalOcrUnavailable('OCR model roles invalid')
    return models


@lru_cache(maxsize=1)
def _engine():
    models = _verified_models()
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR
    from rapidocr.utils.download_file import DownloadFile
    DownloadFile.run = staticmethod(_deny_download)
    return RapidOCR(params={
        # The Chinese 0/180 classifier flips ordinary Korean lines incorrectly.
        # EXIF orientation is handled locally; uncertain/upside-down text needs review.
        'Global.log_level': 'error', 'Global.text_score': 0.0, 'Global.max_side_len': MAX_SIDE,
        'Global.use_cls': False,
        'Global.return_word_box': False, 'Global.return_single_char_box': False,
        'Det.engine_type': EngineType.ONNXRUNTIME, 'Det.lang_type': LangDet.CH,
        'Det.model_type': ModelType.MOBILE, 'Det.ocr_version': OCRVersion.PPOCRV5,
        'Det.model_path': models['detector'], 'Det.limit_side_len': 1280, 'Det.limit_type': 'max',
        'Cls.engine_type': EngineType.ONNXRUNTIME, 'Cls.ocr_version': OCRVersion.PPOCRV4,
        'Cls.model_path': models['orientation_classifier'],
        'Rec.engine_type': EngineType.ONNXRUNTIME, 'Rec.lang_type': LangRec.KOREAN,
        'Rec.model_type': ModelType.MOBILE, 'Rec.ocr_version': OCRVersion.PPOCRV5,
        'Rec.model_path': models['korean_recognizer'],
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'EngineConfig.onnxruntime.use_cuda': False, 'EngineConfig.onnxruntime.use_dml': False,
    })


def extract_image_document(data, *, max_chars=30000):
    result, budget = DocumentText(max_chars=max_chars, units_total=1), TextBudget(max_chars)
    if not isinstance(data, bytes) or not 0 < len(data) <= 20 * 1024 * 1024:
        result.mark('image_read_error')
        return result
    try:
        from PIL import Image, ImageOps
        import numpy as np
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                width, height = original.size
                if min(width, height) <= 0 or max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                    result.mark('image_size_limit')
                    return result
                if original.format not in ('PNG', 'JPEG', 'WEBP'):
                    result.mark('image_read_error')
                    return result
                if getattr(original, 'n_frames', 1) > 1:
                    result.mark('page_limit')
                normalized = ImageOps.exif_transpose(original)
                try:
                    rgba = normalized.convert('RGBA')
                    try:
                        white = Image.new('RGBA', rgba.size, 'white')
                        try:
                            white.alpha_composite(rgba)
                            rgb = white.convert('RGB')
                            try:
                                image = np.asarray(rgb)[:, :, ::-1].copy()
                            finally:
                                rgb.close()
                        finally:
                            white.close()
                    finally:
                        rgba.close()
                finally:
                    normalized.close()
    except ImportError:
        result.mark('parser_unavailable')
        return result
    except Exception:
        result.mark('image_read_error')
        return result
    try:
        engine = _engine()
    except Exception:
        result.mark('ocr_models_unavailable')
        return result
    try:
        output = engine(image)
        result.units_examined = 1
        texts, scores = output.txts or (), output.scores or ()
        for index, text in enumerate(texts):
            if not isinstance(text, str):
                result.mark('ocr_read_error')
                continue
            score = float(scores[index]) if index < len(scores) else 0
            if not math.isfinite(score) or score < 0.75 or score > 1:
                result.mark('ocr_low_confidence')
            if not math.isfinite(score) or score < 0.5 or score > 1:
                result.mark('ocr_omitted_text')
                continue
            if not budget.append(text):
                result.mark('char_limit')
                break
    except Exception:
        result.mark('ocr_read_error')
    result.text = budget.text()
    result.mark('ocr_verification_required' if result.text else 'no_text_image')
    return result
