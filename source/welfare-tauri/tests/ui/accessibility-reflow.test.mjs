import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium}=createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE||'playwright');
const origin=process.env.UI_TEST_ORIGIN||'http://127.0.0.1:1420';
const run=(process.env.ACCESSIBILITY_RUN||`diagnostic-${Date.now()}`).replace(/[^a-zA-Z0-9_-]/g,'_');
const output=path.join(fileURLToPath(new URL('../../../artifacts/accessibility-20260908/',import.meta.url)),run);
const variants=[{width:850,height:700},{width:425,height:350},{width:582,height:700}].flatMap(size=>[100,200].map(font=>({...size,font})));
const pages=['home','reports','sites','search','system','guide','news','policy'];
const selectedPages=process.env.ACCESSIBILITY_PAGES?pages.filter(kind=>process.env.ACCESSIBILITY_PAGES.split(',').includes(kind)):pages;

async function metrics(page){
 return page.locator('main').evaluate(main=>{
  const bounds=main.getBoundingClientRect();
  const selector=el=>el.id?`#${el.id}`:`${el.tagName.toLowerCase()}${el.getAttribute('aria-label')?`[aria-label="${el.getAttribute('aria-label')}"]`:''}${el.className&&typeof el.className==='string'?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):''}`;
  const horizontalScroller=el=>{for(let p=el.parentElement;p&&p!==main;p=p.parentElement){if(['auto','scroll'].includes(getComputedStyle(p).overflowX)&&p.scrollWidth>p.clientWidth+2)return true;}return false;};
  const closedDetail=el=>{for(let parent=el.parentElement;parent&&parent!==main;parent=parent.parentElement){if(parent.tagName==='DETAILS'&&!parent.open&&!parent.querySelector(':scope > summary')?.contains(el))return true;}return false;};
  const overflow=[];
  for(const el of main.querySelectorAll('h1,h2,h3,p,label,button,a,input,select,textarea')){
   const rect=el.getBoundingClientRect(),style=getComputedStyle(el);
   if(!rect.width||!rect.height||style.visibility==='hidden'||el.closest('[aria-hidden="true"]')||horizontalScroller(el)||closedDetail(el))continue;
   if(rect.left<bounds.left-2||rect.right>bounds.right+2)overflow.push({selector:selector(el),text:(el.textContent||el.getAttribute('aria-label')||'').trim().slice(0,70),left:rect.left,right:rect.right,width:rect.width,mainLeft:bounds.left,mainRight:bounds.right});
  }
  const before=main.scrollTop,needsScroll=main.scrollHeight>main.clientHeight+2;
  main.scrollTop=main.scrollHeight;const scrollWorked=!needsScroll||main.scrollTop>0;main.scrollTop=before;
  return {viewport:{width:innerWidth,height:innerHeight},rootFontSize:getComputedStyle(document.documentElement).fontSize,
   documentWidth:document.documentElement.scrollWidth,mainWidth:main.clientWidth,mainScrollWidth:main.scrollWidth,
   mainHeight:main.clientHeight,mainScrollHeight:main.scrollHeight,needsScroll,scrollWorked,overflow:overflow.slice(0,12)};
 });
}

async function reachable(locator,{allowVerticalScan=false}={}){
 await locator.scrollIntoViewIfNeeded();
 if(!(await locator.isDisabled().catch(()=>false)))await locator.focus();
 const result=await locator.evaluate(el=>{
  const r=el.getBoundingClientRect(),ownStyle=getComputedStyle(el);let left=0,right=innerWidth,top=0,bottom=innerHeight;
  for(let parent=el.parentElement;parent;parent=parent.parentElement){const style=getComputedStyle(parent),p=parent.getBoundingClientRect();
   if(['auto','scroll','hidden','clip'].includes(style.overflowX)){left=Math.max(left,p.left);right=Math.min(right,p.right);}
   if(['auto','scroll','hidden','clip'].includes(style.overflowY)){top=Math.max(top,p.top);bottom=Math.min(bottom,p.bottom);}
  }
  const x=Math.max(left,Math.min(right,(r.left+r.right)/2)),y=Math.max(top,Math.min(bottom,(r.top+r.bottom)/2));
  const hit=document.elementFromPoint(x,y);
  return {text:(el.textContent||el.getAttribute('aria-label')||el.title||'').trim().slice(0,70),
   rect:{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height},clip:{left,right,top,bottom},
   fits:r.left>=left-2&&r.right<=right+2&&r.top>=top-2&&r.bottom<=bottom+2,
   contentFits:['INPUT','SELECT','TEXTAREA'].includes(el.tagName)||el.scrollWidth<=el.clientWidth+2,
   textEntry:el.tagName==='TEXTAREA'||(el.tagName==='INPUT'&&['text','search','url'].includes(el.type))?{availableWidth:el.clientWidth-parseFloat(ownStyle.paddingLeft)-parseFloat(ownStyle.paddingRight),twoCharacters:2*parseFloat(ownStyle.fontSize)}:null,
   unobscured:!!hit&&(hit===el||el.contains(hit)),focused:el===document.activeElement,disabled:el.disabled===true};
 });
 if(allowVerticalScan&&!result.fits){
  // A long news title is allowed to need vertical scrolling. Prove both ends
  // can be read and hit-tested; do not require the whole title on one screen.
  result.verticalScan={};
  for(const edge of ['start','end']){
   result.verticalScan[edge]=await locator.evaluate((el,edge)=>{
    el.scrollIntoView({block:edge,inline:'nearest',behavior:'instant'});
    el.focus({preventScroll:true});
    const rect=el.getBoundingClientRect(),line=parseFloat(getComputedStyle(el).lineHeight)||24;
    const x=rect.left+Math.min(rect.width/2,30),y=edge==='start'?rect.top+Math.min(line/2,rect.height/2):rect.bottom-Math.min(line/2,rect.height/2);
    const hit=document.elementFromPoint(x,y);
    return {top:rect.top,bottom:rect.bottom,point:{x,y},readable:y>=0&&y<=innerHeight&&!!hit&&(hit===el||el.contains(hit)),focused:el===document.activeElement};
   },edge);
  }
  result.reachableByVerticalScroll=result.verticalScan.start.readable&&result.verticalScan.end.readable;
 }
 if(!result.disabled){await locator.click({trial:true});result.clickActionable=true;}
 return result;
}

function critical(page,kind){
 const main=page.locator('main');
 const button=name=>main.getByRole('button',{name,exact:true});
 if(kind==='home')return [main.getByRole('link',{name:'정책 변경 확인하기',exact:true}),main.getByRole('link',{name:'새 소식 탐색하기',exact:true})];
 if(kind==='reports')return [main.getByLabel('카테고리',{exact:true}),main.getByLabel('사이트',{exact:true}),main.getByRole('button',{name:/CSV/}).first()];
 if(kind==='sites')return [main.getByRole('textbox',{name:'수집할 홈페이지 또는 게시판 주소',exact:true}),button('분석 없이 등록'),button('시험 복지 지원 기관 수집 규칙 테스트')];
 if(kind==='search')return [main.getByRole('textbox',{name:'복지 정보 검색어',exact:true}),button('검색')];
 if(kind==='system')return [button('백업 받기'),main.locator('input[type=file][accept=".db"]')];
 if(kind==='guide')return [main.getByRole('link',{name:'정책 최신화 준비',exact:true}),main.getByRole('link',{name:'새 소식 탐색 준비',exact:true})];
 if(kind==='news')return [main.locator('[data-news-item]').first().getByRole('checkbox'),main.locator('[data-news-item]').first().getByRole('button',{name:/전체 내용 미리보기$/})];
 return [button('정책 정보 최신인지 확인하기'),button('세부 작업'),main.getByRole('button',{name:/선택한 내용 Notion에 반영/}).first()];
}

test('합성 화면의 CSS 뷰포트 850/425/582px 및 html 글자크기 100/200% 재배치 (실제 브라우저 줌 아님)',async t=>{
 await mkdir(output,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const results=[];
 try{
  for(const kind of selectedPages)for(const variant of variants){
   const id=`${kind}-${variant.width}x${variant.height}-font${variant.font}`;
   await t.test(id,async()=>{
    const page=await browser.newPage({viewport:{width:variant.width,height:variant.height},serviceWorkers:'block'});
    const errors=[],blocked=[];page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/*',route=>{const url=new URL(route.request().url());if(url.origin===origin&&!url.pathname.startsWith('/api/'))return route.continue();blocked.push({origin:url.origin,path:url.pathname});return route.abort();});
    page.setDefaultTimeout(7000);
    const result={id,method:'CSS viewport and html font-size; no browser zoom',variant};
    try{
     await page.goto(`${origin}/tests/ui/accessibility-reflow.html?page=${kind}`);
     await page.locator('main h1').first().waitFor();
     await page.evaluate(font=>{document.documentElement.style.fontSize=`${font}%`;},variant.font);
     if(kind==='search'){
      await page.getByRole('textbox',{name:'복지 정보 검색어',exact:true}).fill('지원');await page.getByRole('button',{name:'검색',exact:true}).click();await page.getByRole('heading',{name:'검색 결과',exact:true}).waitFor();
     }else if(kind==='sites')await page.getByRole('heading',{name:'시험 복지 지원 기관',exact:true}).waitFor();
     else if(kind==='news')await page.locator('[data-news-item]').first().waitFor();
     else if(kind==='policy')await page.getByRole('button',{name:'정책 정보 최신인지 확인하기',exact:true}).waitFor();
     // Let ResizeObserver charts and initial motion settle without dispatching jobs.
     await page.waitForTimeout(450);
     result.layout=await metrics(page);result.controls=[];
     if(kind==='search')result.search=await page.locator('main .scrollbar-thin').evaluate(el=>({height:el.clientHeight,scrollHeight:el.scrollHeight,resultHeadings:el.querySelectorAll('h3').length}));
     await page.screenshot({path:path.join(output,id+'-before-controls.png'),animations:'disabled'});
     for(const [index,locator] of critical(page,kind).entries())result.controls.push(await reachable(locator,{allowVerticalScan:kind==='news'&&index===1}));
     result.fixture=await page.evaluate(()=>window.a11yFixture);
     result.errors=errors;result.blockedNetwork=blocked;
     await page.screenshot({path:path.join(output,id+'.png'),animations:'disabled'});
     assert.deepEqual(errors,[],'page errors');assert.deepEqual(result.fixture.unexpected,[],'fixture must cover every API call');
     assert.ok(result.layout.documentWidth<=variant.width+2,'document horizontal overflow');
     assert.ok(result.layout.mainScrollWidth<=result.layout.mainWidth+2,JSON.stringify(result.layout));
     assert.deepEqual(result.layout.overflow,[],JSON.stringify(result.layout.overflow));
     assert.equal(result.layout.scrollWorked,true,'main content remains vertically scrollable');
     if(result.search){assert.ok(result.search.height>0,JSON.stringify(result.search));assert.ok(result.search.resultHeadings>0,'synthetic search result must render after search');}
     for(const control of result.controls){assert.ok(control.fits||control.reachableByVerticalScroll,JSON.stringify(control));assert.equal(control.unobscured,true,JSON.stringify(control));assert.equal(control.contentFits,true,JSON.stringify(control));assert.ok(control.disabled||control.focused,JSON.stringify(control));if(control.textEntry)assert.ok(control.textEntry.availableWidth>=control.textEntry.twoCharacters,JSON.stringify(control));}
     result.passed=true;
    }catch(error){result.passed=false;result.failure=String(error.message);await page.screenshot({path:path.join(output,id+'-failure.png'),animations:'disabled'}).catch(()=>{});throw error;}
    finally{results.push(result);await writeFile(path.join(output,id+'.json'),JSON.stringify(result,null,2),'utf8');await page.close();}
   });
  }
 }finally{await browser.close();await writeFile(path.join(output,'summary.json'),JSON.stringify({method:'CSS viewport dimensions and html font-size only; NOT actual browser zoom',results},null,2),'utf8');}
});
