import {test} from 'node:test';
import assert from 'node:assert/strict';
import {markdownBlocks,markdownInline} from '../src/utils/readableMarkdown.js';
test('스크린샷의 제목과 목록을 서식 토큰으로 표시',()=>{
    assert.deepEqual(markdownBlocks('### ✅ 지원 내용\n- 프로그램 참여\n\n### 📋 신청 자격\n- 아동'),[
        {type:'heading',text:'✅ 지원 내용'},{type:'list',ordered:false,start:undefined,items:['프로그램 참여']},
        {type:'heading',text:'📋 신청 자격'},{type:'list',ordered:false,start:undefined,items:['아동']}]);
});
test('일반 텍스트·번호 목록·줄바꿈·강조·긴 본문을 보존',()=>{
    assert.equal(markdownBlocks('### C#')[0].text,'C#');
    assert.equal(markdownBlocks('### 신청 방법 ###')[0].text,'신청 방법');
    assert.equal(markdownBlocks('내용\n두 번째 줄')[0].text,'내용\n두 번째 줄');
    assert.equal(markdownBlocks('3. 첫 항목\n4. 다음 항목')[0].start,3);
    assert.equal(markdownInline('**중요**')[0].type,'strong');
    assert.equal(markdownBlocks('문장<br/>다음')[0].text,'문장\n다음');
    const long='가'.repeat(20000);assert.equal(markdownBlocks(long)[0].text,long);
});
test('HTML·위험 링크는 실행용 토큰이 되지 않고 코드는 원문 보존',()=>{
    for(const value of ['<script>alert(1)</script>','[실행](javascript:alert(1))','![외부 이미지](https://example.invalid/x.png)']) {
        assert.equal(markdownBlocks(value)[0].type,'paragraph');
        assert.equal(markdownInline(value).some(part=>part.type==='html'||part.type==='image'),false);
    }
    assert.equal(markdownInline('[실행](javascript:alert(1))').some(part=>part.type==='link'),false);
    assert.deepEqual(markdownBlocks('```\n### literal\n```'),[{type:'code',text:'### literal'}]);
});
