import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import NewsResultDetails from '../../src/components/NewsResultDetails';
import {welfareApi} from '../../src/api/welfare';
import '../../src/index.css';

for (const key of Object.keys(welfareApi)) if (typeof welfareApi[key]==='function') welfareApi[key]=async()=>{throw Error(`실제 호출 금지: ${key}`);};
window.calls=[];window.pending=[];
const tail='마지막 조건: 중복 지원은 제외합니다. <script>window.untrustedExecuted=true</script>';
const whole='검증된 내용\n'+'지원 안내 '.repeat(2600)+'\n'+tail;
welfareApi.getNewsResultDetails=(profileId,recordId,{signal})=>new Promise((resolve,reject)=>{
    const call={profileId,recordId,aborted:false};window.calls.push(call);
    signal.addEventListener('abort',()=>{call.aborted=true;});
    window.pending.push({resolve:value=>resolve({record_id:recordId,details:{support_detail:value||whole},detail_omissions:{},ai_called:false}),
        reject:()=>reject(Error('합성 조회 실패'))});
});
const base={record_id:'job:first',revision:1,details:{category:'교육'},
    detail_omissions:{support_detail:{length:whole.length,available:true,reason:'deferred'}}};
function Fixture(){
    const [item,setItem]=useState(base),[profile,setProfile]=useState('p');
    return <main className="min-h-screen bg-gray-50 p-4 text-gray-900 dark:bg-gray-900 dark:text-gray-100">
        <div className="flex flex-wrap gap-2">
            <button onClick={()=>window.pending.shift()?.resolve()}>응답 도착</button>
            <button onClick={()=>window.pending.shift()?.reject()}>응답 실패</button>
            <button onClick={()=>{setProfile('other');setItem({...base,record_id:'job:other'});}}>다른 기록</button>
            <button onClick={()=>setItem({...item,details:{category:'변경된 분류'}})}>동일 기록 갱신</button>
        </div>
        <NewsResultDetails profileId={profile} item={item}/>
        <NewsResultDetails profileId="p" item={{record_id:'official:a:b',details:{support_detail:'짧은 공식 내용'}}}/>
        <NewsResultDetails profileId="p" item={{record_id:'job:oversize',details:{},
            detail_omissions:{support_detail:{length:30001,available:false,reason:'display_limit'}}}}/>
    </main>;
}
createRoot(document.getElementById('root')).render(<Fixture/>);
