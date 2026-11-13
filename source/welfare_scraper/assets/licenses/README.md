# 추가 동봉 고지

2026-09-18 backend 점검에서 누락이 확인된 ONNX Runtime, pandas, pypdf, FastAPI, PyYAML, ANTLR의 고지 전문입니다. `manifest.json`에는 사용한 버전과 파일의 크기·SHA-256, 동봉 경로를 기록했습니다.

ONNX Runtime의 LICENSE·ThirdPartyNotices와 pandas·pypdf·FastAPI·PyYAML의 LICENSE는 빌드에 사용하는 고정 버전 wheel 원본을 그대로 포함합니다. ANTLR은 설치된 Python runtime 4.9.3에 대응하는 [공식 4.9.3 LICENSE.txt](https://github.com/antlr/antlr4/blob/4.9.3/LICENSE.txt)를 수정 없이 포함합니다. 이 파일에 함께 있는 JavaScript 관련 MIT 고지도 원본 그대로 보존했습니다.

이 폴더는 확인된 누락 고지의 보완 범위이며, 모든 의존성 목록이나 모든 재배포 조건의 충족을 뜻하지 않습니다. 앱 전체에 새로운 라이선스를 부여하지 않으며, GEOS의 대응 소스·교체 또는 재링크 제공 여부는 이 작업에 포함되지 않습니다.
