import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createSettingsDraftStore } from '../src/utils/settingsDrafts.js';
const setup = () => { const s=createSettingsDraftStore();s.ensure('region','');s.ensure('key','');s.hydrate({region:'도봉',key:''});return s; };

test('서버 조회는 미저장 입력을 덮지 않는다', () => {
    const s=setup();s.edit('region','노원');s.hydrate({region:'도봉'});
    assert.equal(s.get('region'),'노원');assert.equal(s.dirty('region'),true);
});
test('다른 영역 저장 성공 뒤 전체 조회에도 편집을 보존한다', () => {
    const s=setup();s.edit('region','노원');s.edit('key','test-secret');
    s.commit('key','test-secret','');s.hydrate({region:'도봉',key:'저장됨'});
    assert.equal(s.get('region'),'노원');assert.deepEqual(s.dirtyNames(),['region']);
});
test('이전 조회 응답이 뒤늦게 도착해도 저장된 새 값을 되돌리지 않는다', () => {
    const s=setup(), request=s.capture();s.edit('region','노원');s.commit('region','노원');
    s.hydrate({region:'도봉'},request);assert.equal(s.get('region'),'노원');assert.equal(s.dirty('region'),false);
});
test('저장 도중 다시 수정한 입력은 성공 응답 후에도 남는다', () => {
    const s=setup();s.edit('region','노원');s.edit('region','강북');s.commit('region','노원');
    assert.equal(s.get('region'),'강북');assert.equal(s.dirty('region'),true);
});
test('화면 재진입 초기값과 저장 실패는 편집을 초기화하지 않는다', () => {
    const s=setup();s.edit('region','노원');s.ensure('region','도봉');
    assert.equal(s.get('region'),'노원');assert.deepEqual(s.dirtyNames(),['region']);
});
test('미저장 취소는 확인 이후 새로 수정한 필드를 버리지 않는다', () => {
    const s=setup();s.edit('region','노원');s.edit('key','draft');const request=s.capture();s.edit('key','newer');s.discard(request);
    assert.equal(s.get('region'),'도봉');assert.equal(s.get('key'),'newer');assert.deepEqual(s.dirtyNames(),['key']);
});
test('키 저장 성공은 비밀 값을 baseline에 남기지 않는다', () => {
    const s=setup();s.edit('key','secret');s.commit('key','secret','');s.edit('key','new');s.discard(s.capture());
    assert.equal(s.get('key'),'');assert.deepEqual(s.dirtyNames(),[]);
});
test('원래 값으로 돌아오면 미저장 경고가 사라진다', () => {
    const s=setup();s.edit('region','노원');s.edit('region','도봉');assert.deepEqual(s.dirtyNames(),[]);
});
test('서버의 다음 실행 시각 차이만으로는 미저장으로 판단하지 않는다', () => {
    const s=setup();s.ensure('schedule',{enabled:false,next_run:null});
    s.edit('schedule',{enabled:false,next_run:'2030-01-01'});
    assert.equal(s.dirty('schedule'),false);
});

test('화면 이동 중에도 중복 저장을 차단하고 실패 후 잠금을 해제한다', async () => {
    const s=setup();let reject;
    const pending=s.runWrite(()=>new Promise((resolve,r)=>{reject=r;}));
    assert.equal(s.isWriting(),true);
    await assert.rejects(s.runWrite(()=>Promise.resolve()),/다른 설정/);
    reject(Error('failure'));await assert.rejects(pending,/failure/);
    assert.equal(s.isWriting(),false);
    assert.equal(await s.runWrite(()=>Promise.resolve('ok')),'ok');
});

test('실제 EXE 종료 함수는 미저장 또는 저장 중이면 종료 명령을 보내지 않는다', async () => {
    const source=fs.readFileSync(new URL('../src/components/DesktopLifecycle.jsx',import.meta.url),'utf8');
    const start=source.indexOf('const begin = async () => {');
    const body=source.slice(start,source.indexOf('\n    useEffect(',start));
    const s=setup(), modeRef={current:null};let calls=0;
    const begin=new Function('settingsDrafts','modeRef','requestRef','changeMode','setError','setElapsed','command',`${body};return begin;`)(
        s,modeRef,{current:false},mode=>{modeRef.current=mode;},()=>{},()=>{},async()=>{calls++;});
    s.edit('region','노원');await begin();assert.equal(calls,0);assert.equal(modeRef.current,'confirm');
    modeRef.current=null;await begin();assert.equal(calls,0); // 창 유지 후 다시 확인
    modeRef.current='confirm';let done;const write=s.runWrite(()=>new Promise(r=>{done=r;}));
    await begin();assert.equal(calls,0);assert.equal(modeRef.current,'error');
    done();await write;modeRef.current='confirm';await begin();assert.equal(calls,1);
});
test('미저장 키를 디스크에 쓰지 않고 EXE 종료 전에도 확인한다', () => {
    const read = p => fs.readFileSync(new URL(p,import.meta.url),'utf8');
    assert.doesNotMatch(read('../src/utils/settingsDrafts.js'),/localStorage|sessionStorage|writeFile/);
    assert.match(read('../src/components/DesktopLifecycle.jsx'),/settingsDrafts.dirtyNames\(\).length && modeRef.current !== 'confirm'/);
    assert.match(read('../src/App.jsx'),/addEventListener\('beforeunload', warn\)/);
    assert.match(read('../src/pages/Settings.jsx'),/!settingsDrafts.dirty\('policyDbs'\)/);
});
