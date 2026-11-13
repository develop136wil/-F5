import test from 'node:test';
import assert from 'node:assert/strict';
import {appendKeywords, restoreKeyword} from '../src/utils/keywordList.js';

test('기존 순서와 원본을 유지하며 쉼표·줄바꿈 추가와 중복 제거를 지원한다',()=>{
    const words=['부모 교육','돌봄'];
    const result=appendKeywords(words,' 돌봄, 바우처\n부모 교육\n양육, 바우처 ');
    assert.deepEqual(result,{words:['부모 교육','돌봄','바우처','양육'],added:['바우처','양육']});
    assert.deepEqual(words,['부모 교육','돌봄']);
    assert.deepEqual(appendKeywords(words,' , \n ').words,words);
});
test('삭제 되돌리기는 원래 위치를 보존하고 재추가된 단어를 복제하지 않는다',()=>{
    const words=['처음','마지막'];
    assert.deepEqual(restoreKeyword(words,{word:'가운데',index:1}),['처음','가운데','마지막']);
    assert.equal(restoreKeyword(words,{word:'처음',index:0}),words);
    assert.deepEqual(restoreKeyword([],{word:'마지막',index:72}),['마지막']);
});
