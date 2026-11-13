"""Generate synthetic gallery UI cases in a NEW workspace directory, without a DB/API.

The failure.html page simulates clipboard denial only inside that test file.
Serve the output directory on localhost with the specified port to view test posters.
"""
import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('--port', type=int, default=1432)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if workspace not in root.parents or root.exists():
        parser.error('Use a new output directory inside the workspace')
    sys.path.insert(0, str(workspace / 'welfare_scraper'))
    from services.news_gallery import generate_html

    root.mkdir(parents=True)
    base = {'title': '우리 동네 가족 체험 행사', 'org': '시험 기관', 'event_date': '2026-09-10',
            'support_detail': '가족과 함께하는 체험 행사입니다.\n신청 방법과 준비물은 원문을 확인해주세요.',
            'link': 'https://example.invalid/test-gallery', 'ai_data': {'source_input': {
                'status': 'partial', 'note': '첨부 안내의 일부를 확인하지 못했습니다.'}}}
    poster = {**base, 'title': '포스터 전체가 잘리지 않는지 확인',
              'poster_url': f'http://127.0.0.1:{args.port}/poster.svg'}
    broken = {**base, 'title': '이미지를 열 수 없는 게시물',
              'poster_url': f'http://127.0.0.1:{args.port}/missing.png'}
    long = {**base, 'title': '긴제목과기관명도화면을벗어나지않습니다' * 10,
            'org': '매우긴기관이름' * 15, 'support_detail': '내용이긴경우에도문자단위로줄바꿈합니다' * 120}
    cases = {'one': [base], 'two': [poster, broken], 'many': [base, poster, broken, long, base, poster],
             'long': [long], 'empty': [], 'failure': [base]}
    for name, items in cases.items():
        generated = Path(generate_html(items))
        try:
            content = generated.read_text(encoding='utf-8')
        finally:
            generated.unlink()  # Only the tempfile returned by this invocation.
        if name == 'failure':
            content = content.replace('<script>', '''<script>
// Test-only mocks: this synthetic page NEVER writes to the system clipboard.
Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async()=>{throw Error('test denial')}}});
document.execCommand=()=>false;
</script><script>''', 1)
        (root / f'{name}.html').write_text(content, encoding='utf-8')
    (root / 'poster.svg').write_text('''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="500">
<rect width="300" height="500" fill="#e0edff"/><rect x="10" y="10" width="280" height="480" fill="none" stroke="#2563eb" stroke-width="5"/>
<text x="150" y="45" text-anchor="middle" font-size="22">포스터 상단</text>
<text x="150" y="250" text-anchor="middle" font-size="24">시험용 포스터</text>
<text x="150" y="475" text-anchor="middle" font-size="22">포스터 하단</text></svg>''', encoding='utf-8')
    (root / 'index.html').write_text('''<!doctype html><html lang="ko"><meta charset="utf-8">
<title>격리 HTML 화면 시험</title><style>body{font:16px sans-serif;margin:16px}button,select{padding:8px;margin:4px}iframe{display:block;border:1px solid #ccc;height:720px}</style>
<h1>가짜 게시물 화면 시험</h1><p>실제 수집·AI 호출·Notion 등록 없음. 복사 실패 표본은 클립보드도 변경하지 않습니다.</p>
<label>표본 <select aria-label="표본" onchange="sample.src=this.value+'.html'">
<option value="one">1건</option><option value="two">2건·포스터</option><option value="many">6건</option>
<option value="long">긴 제목</option><option value="empty">0건</option><option value="failure">복사 실패</option></select></label>
<button onclick="resizeSample(1200)">1200px</button><button onclick="resizeSample(800)">800px</button><button onclick="resizeSample(480)">480px</button><button onclick="resizeSample(320)">320px</button>
<p><output id="metrics" aria-label="화면 측정 결과"></output></p>
<iframe id="sample" title="새소식 HTML 시험 화면" src="one.html" style="width:1200px" onload="measure()"></iframe>
<script>
function measure(){const d=sample.contentDocument;const rects=[...d.querySelectorAll('.card')].map(x=>x.getBoundingClientRect());
metrics.textContent=JSON.stringify({viewport:d.documentElement.clientWidth,scrollWidth:d.documentElement.scrollWidth,cards:rects.length,
maxCardWidth:Math.max(0,...rects.map(x=>x.width)),maxCardHeight:Math.max(0,...rects.map(x=>x.height)),
mediaHeights:[...d.querySelectorAll('.media')].map(x=>x.getBoundingClientRect().height),
overflowing:[...d.querySelectorAll('.card,h2,.review,.eyebrow,.actions')].filter(x=>x.scrollWidth>x.clientWidth+1).length});}
function resizeSample(width){sample.style.width=width+'px';requestAnimationFrame(measure);}
</script></html>''', encoding='utf-8')
    print(f'Synthetic gallery UI cases: {root}')


if __name__ == '__main__':
    main()
