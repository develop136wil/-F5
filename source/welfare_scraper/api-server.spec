# -*- mode: python ; coding: utf-8 -*-


from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs, copy_metadata
from pathlib import Path
import hashlib
import json
import runpy

# Chromium은 번들하지 않고 첫 실행 시 scraper_engine._install_browser()가 자동 설치한다.
# (설치본 용량 절감 — recipient는 최초 수집 시 1회 다운로드)

# APScheduler는 실행기/트리거/저장소를 동적 import → PyInstaller 정적분석이 못 잡으므로
# 전체 서브모듈을 명시적으로 수집한다. (인앱 스케줄링용)
_apscheduler_imports = collect_submodules('apscheduler')

# OCR models are versioned build inputs. A missing or altered model must stop a
# release build, not cause a hidden model download on the recipient's computer.
_model_root = Path(SPECPATH) / 'assets' / 'local_ocr'
_manifest = json.loads((_model_root / 'model_manifest.json').read_text(encoding='utf-8'))
for _model in _manifest['models']:
    _path = _model_root / _model['filename']
    if (_path.parent != _model_root or not _path.is_file()
            or _path.stat().st_size != _model['size_bytes']
            or hashlib.sha256(_path.read_bytes()).hexdigest() != _model['sha256']):
        raise RuntimeError('Pinned local OCR model is missing or invalid')
_document_data = [('assets/local_ocr', 'assets/local_ocr')]
_notice_collector = runpy.run_path(str(Path(SPECPATH) / 'scripts' / 'license_bundle_inputs.py'))
_document_data += _notice_collector['collect_license_data'](SPECPATH)
_document_data += collect_data_files('rapidocr', excludes=['models/*'])
_document_data += collect_data_files('pypdfium2_raw')
_document_data += collect_data_files('pypdfium2')
for _distribution in ('olefile', 'pypdfium2', 'rapidocr', 'onnxruntime', 'Pillow',
                       'pyclipper', 'shapely', 'opencv-python', 'omegaconf', 'antlr4-python3-runtime'):
    _document_data += copy_metadata(_distribution)
_document_imports = collect_submodules('rapidocr', filter=lambda name: not any(
    part in name for part in ('.openvino', '.paddle', '.torch', '.pytorch', '.tensorrt', '.mnn')))
_document_imports += ['pypdfium2', 'pypdfium2_raw', 'olefile', 'onnxruntime',
                       'PIL.Image', 'PIL.ImageOps', 'services.local_document_worker',
                       'services.hwp_text', 'services.local_ocr', 'services.local_pdf_ocr']

a = Analysis(
    ['api_server.py'],
    pathex=[],
    binaries=collect_dynamic_libs('onnxruntime') + collect_dynamic_libs('pypdfium2_raw'),
    datas=[('assets/site_import_template.xlsx', 'assets')] + _document_data,
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'engineio.async_drivers.aiohttp',
        'openpyxl',
        'pandas',
        'lxml',
        'plyer',
        'plyer.platforms.win.notification',
        'multipart',
        'python_multipart',
        # 의미검색/임베딩 (numpy) + 인앱 스케줄링(apscheduler + 시간대 라이브러리)
        'numpy',
        'tzlocal',
        'tzdata',
        'pytz',
    ] + _apscheduler_imports + _document_imports,
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=[],
    # CSV/XLSX use pandas' C parser and openpyxl. Arrow/Parquet is not an app
    # feature; pandas treats pyarrow as optional. Avoid extracting its native
    # libraries on every one-file backend launch. Keep pandas/numpy/OCR intact.
    excludes=['tkinter', 'unittest', 'test', 'setuptools', 'pip', 'wheel', 'pytest', 'pytest_asyncio', '_pytest', 'pyarrow'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='api-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 배포용: UPX 패킹은 백신 오탐·시작 지연을 유발하므로 비활성화
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
