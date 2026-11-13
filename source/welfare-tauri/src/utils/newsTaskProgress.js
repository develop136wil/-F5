import {workflowNotice, workflowStatusText} from './newsWorkflowState.js';

const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const number = value => value.toLocaleString('ko-KR');
const metric = (done, total) => {
    done=count(done); total=count(total);
    if (done===null || total===null || done>total) return null;
    return {done,total,percent:total>0?Math.floor(done/total*100):null};
};

// Percentages always refer to the labelled phase, never elapsed time or an
// invented whole-run denominator. Older workflows remain text-only compatible.
export function newsTaskProgress(task = {}, unavailable = false) {
    const value=task.progress?.schema_version===1?task.progress:null;
    const phase=value?.phase;
    const active=!unavailable && !['complete','waiting','failed','cancelled'].includes(task.state)
        && (task.active || ['ready','running'].includes(task.state));
    const labels={sites:'기관 게시판 확인',content:'본문·첨부 확인',analysis:'수집 자료 분석'};
    const phaseLabel=labels[phase] || (task.stage==='analyzing'?'수집 자료 분석':'새 소식 수집');
    const gauge=phase==='sites'?metric(value.sites_processed,value.sites_total)
        :phase==='content'?metric(value.site_documents_processed,value.site_documents_total)
        :phase==='analysis'?metric(value.analysis_processed,value.analysis_total):null;
    const unit=phase==='sites'?'개 사이트':'건';
    const scope=phase==='content'?'이 기관 자료':phase==='analysis'?'분석 처리':'기관 확인';
    let amount=gauge?`${scope} ${number(gauge.done)}/${number(gauge.total)}${unit}`:'처리 건수를 확인하고 있습니다.';
    if(gauge?.total===0) amount=`${scope} 대상 없음`;
    if(!value&&!active&&!unavailable) amount='이전 실행에는 상세 진행 기록이 없습니다.';
    const lines=[];
    const sites=value&&metric(value.sites_processed,value.sites_total);
    if(phase==='content'&&sites)lines.push(`기관 확인 ${number(sites.done)}/${number(sites.total)}개 사이트`);
    if(phase==='analysis') {
        const remaining=count(value.analysis_remaining),review=count(value.analysis_review_required),failed=count(value.analysis_failed);
        if(remaining!==null&&remaining>0)lines.push(`남은 분석 ${number(remaining)}건`);
        if(review!==null&&review>0)lines.push(`직접 확인 필요 ${number(review)}건`);
        if(failed!==null&&failed>0)lines.push(`분석 실패 ${number(failed)}건`);
    } else if(value) {
        const documents=count(value.documents_processed),attachments=count(value.attachments_processed);
        if(documents!==null&&documents>0)lines.push(`본문 확인 ${number(documents)}건`);
        if(attachments!==null&&attachments>0)lines.push(`첨부 확인 ${number(attachments)}개`);
    }
    const siteIssues=count(value?.sites_review_required);
    if(siteIssues!==null&&siteIssues>0)lines.push(`추가 확인 ${number(siteIssues)}개 사이트`);
    const siteName=['sites','content'].includes(phase)&&typeof value.site_name==='string'?value.site_name.slice(0,120):'';
    return {
        phaseLabel,amount,percent:gauge?.percent??null,active:Boolean(active),lines,siteName,
        title:unavailable?'진행 상태 확인 지연':active?phaseLabel:workflowStatusText(task),
        note:unavailable?'연결을 확인 중입니다. 아래 수치는 마지막으로 확인한 값입니다.':workflowNotice(task),
        phaseNote:gauge?.total>0?'현재 단계의 처리 비율입니다. 전체 작업 완료율·남은 시간과는 다릅니다.':'',
    };
}

export const newsTaskPollInterval = items => Array.isArray(items)
    && items.some(item => !['complete','waiting','failed','cancelled'].includes(item.state)
        && (item.active || ['running','ready'].includes(item.state))) ? 2000 : 10000;

// The sidebar is a glanceable summary; full reasons and actions stay on the
// news screen. A phase's count must never be presented as overall completion.
export function compactNewsTaskStatus(task = {}, unavailable = false) {
    if(unavailable)return '연결 확인 중';
    const view=newsTaskProgress(task);
    if(view.active)return task.stage==='analyzing'?'분석 중':'수집 중';
    if(['waiting','failed'].includes(task.state)) {
        const reason=task.error_code||task.summary?.analysis?.stop_reason;
        if(reason==='daily_quota_exhausted')return 'AI 한도 대기';
        if(reason==='local_budget_exhausted')return '이어서 진행 가능';
        if(task.state==='waiting'&&task.stage==='analyzing'&&task.summary?.collection?.state==='finished')return '분석 대기';
        return task.state==='failed'?'확인 필요':'잠시 멈춤';
    }
    if(task.state==='complete')return view.title==='완료'?'완료':'완료 · 확인 필요';
    if(task.state==='cancelled')return '중지됨';
    return '상태 확인 필요';
}
