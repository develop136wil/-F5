import test from 'node:test';
import assert from 'node:assert/strict';
import config from '../tailwind.config.js';
import { shouldHandleSearchShortcut } from '../src/utils/searchShortcut.js';

test('검색 단축키는 한글 조합 중·이미 처리된 키·대화상자에서 작동하지 않는다', () => {
    for (const key of ['/', 'k']) {
        const event = {key, ctrlKey: key === 'k', target: {tagName: 'BODY'}};
        for (const blocked of [{isComposing:true}, {keyCode:229}, {defaultPrevented:true}]) {
            assert.equal(shouldHandleSearchShortcut({...event, ...blocked}), false);
        }
        assert.equal(shouldHandleSearchShortcut(event, true), false);
    }
});

test('입력·선택·편집 영역의 슬래시는 보존하고 기존 Ctrl/Cmd+K를 유지한다', () => {
    for (const target of [{tagName:'INPUT'}, {tagName:'TEXTAREA'}, {tagName:'SELECT'}, {isContentEditable:true}]) {
        assert.equal(shouldHandleSearchShortcut({key:'/', target}), false);
        assert.equal(shouldHandleSearchShortcut({key:'k', ctrlKey:true, target}), true);
    }
    assert.equal(shouldHandleSearchShortcut({key:'K', metaKey:true}), true);
    assert.equal(shouldHandleSearchShortcut({key:'/'}), true);
    assert.equal(shouldHandleSearchShortcut({key:'/', altKey:true}), false);
    assert.equal(shouldHandleSearchShortcut({key:'k'}), false);
});

const rgb = hex => hex.match(/[a-f\d]{2}/gi).map(value => parseInt(value,16));
const luminance = color => color.map(value => value/255).map(value => value <= .04045 ? value/12.92 : ((value+.055)/1.055)**2.4).reduce((sum,value,index) => sum+value*[.2126,.7152,.0722][index],0);
const contrast = (a,b) => (Math.max(luminance(a),luminance(b))+.05)/(Math.min(luminance(a),luminance(b))+.05);
test('공통 강조색·보조 본문색의 지정된 밝은 배경 조합은 4.5:1 이상이다', () => {
    const colors=config.theme.extend.colors;
    const primary=rgb(colors.primary), secondary=rgb(colors['text-secondary']);
    const white=rgb(colors.surface), background=rgb(colors.background);
    const selected=primary.map((value,index)=>value*.1+white[index]*.9);
    for (const [label,a,b] of [
        ['흰색/강조색',white,primary], ['강조색/선택 배경',primary,selected],
        ['보조 본문/흰색',secondary,white], ['보조 본문/페이지 배경',secondary,background],
    ]) assert.ok(contrast(a,b)>=4.5, `${label}: ${contrast(a,b)}`);
});
