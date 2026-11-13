import test from 'node:test';
import assert from 'node:assert/strict';
import {missingProfileSiteIds,normalizeSiteIds,toggleProfileSite} from '../src/utils/newsProfileSites.js';

test('사이트 ID를 문자열로 정규화하고 순서를 유지하며 중복을 제거한다',()=>{
    assert.deepEqual(normalizeSiteIds([1,'2','1','',null,undefined]),['1','2']);
    assert.deepEqual(normalizeSiteIds(null),[]);
});
test('사이트 하나를 선택·해제해도 다른 선택을 보존한다',()=>{
    assert.deepEqual(toggleProfileSite(['1'],'2',true),['1','2']);
    assert.deepEqual(toggleProfileSite(['1','2'],'1',false),['2']);
});
test('삭제됐거나 현재 목록에 없는 사이트만 구분한다',()=>{
    assert.deepEqual(missingProfileSiteIds(['1','old'],[{id:1,name:'현재'}]),['old']);
});
