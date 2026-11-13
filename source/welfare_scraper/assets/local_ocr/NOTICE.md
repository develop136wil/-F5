# Local Korean document extraction

This directory contains fixed RapidAI PP-OCRv5 detector/Korean recognition models and a PP-OCRv4 orientation model. The Chinese orientation classifier is disabled for Korean text after a regression test found incorrect 180-degree rotation of upright Korean lines. EXIF orientation is respected. Models are verified against `model_manifest.json`; the application never downloads a replacement at runtime.

Model publisher: RapidAI / RapidOCR, versioned model repository v3.9.2.
Upstream license declaration: Apache License 2.0 (`licenses/MODEL-SOURCE-README.md`).
RapidOCR license text: `licenses/RapidOCR-Apache-2.0.txt`.
HWP container reader: olefile 0.47, license in `licenses/olefile-LICENSE.txt`.

Runtime dependencies include ONNX Runtime (MIT), pypdfium2 and PDFium, Pillow, OpenCV, Shapely/GEOS, pyclipper/Clipper, OmegaConf and ANTLR runtime. They remain subject to their own licenses. Distribution must provide the required license texts and third-party notices; package metadata alone is not a substitute. This notice does not relicense those components.

OCR extracts text locally but cannot guarantee numbers, dates, exclusions or table relationships. Its output always retains a source-review warning. HWP protection flags are respected, macros and embedded programs are not executed, and partial coverage remains visible.
