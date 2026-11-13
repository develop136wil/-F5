import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// Only the actual API wrapper is evaluated; no HTTP/client/runtime imports.
const source=readFileSync(new URL('../src/api/welfare.js',import.meta.url),'utf8');
const method=source.slice(source.indexOf('    previewPolicyDraftFromUrl: async'),source.indexOf('    createPolicyDraftFromUrl: async'));

test('문서 전송 동의는 기본 false이며 실제 true만 한 요청 본문에 전달한다',async()=>{
    const calls=[], result={ok:true};
    const client={post:async(url,body,config)=>{calls.push({url,body,config});return {data:result};}};
    const api=new Function('client',`return {${method}}`)(client);
    for(const choice of [undefined,false,1,'true',null,true]) {
        assert.equal(await api.previewPolicyDraftFromUrl('https://example.org/source.pdf','test','',choice),result);
        const request=calls.at(-1);
        assert.equal(request.url,'/policy/draft-from-url/preview');
        assert.deepEqual(request.body,{url:'https://example.org/source.pdf',db_key:'test',title:null,privacy_reviewed:choice===true});
        assert.equal(request.config.timeout,300000);
    }
    await api.previewPolicyDraftFromUrl('https://example.org/another.pdf','test');
    assert.equal(calls.at(-1).body.privacy_reviewed,false,'이전 요청 동의를 보관하지 않음');
});
