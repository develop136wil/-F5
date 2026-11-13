import React, { useEffect, useId, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { welfareApi } from '../api/welfare';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import { policyWatchOutcome } from '../utils/policyReviewState';
import { RefreshCw, Plus, Trash2, ExternalLink, Eye, Loader2, X, Bell } from 'lucide-react';

const fmt = (s) => (s ? new Date(s).toLocaleString('ko-KR') : '-');

const PolicyWatch = ({ embedded = false }) => {
    const Heading = embedded ? 'h2' : 'h1';
    const queryClient = useQueryClient();
    const [checking, setChecking] = useState(false);
    const [adding, setAdding] = useState(false);
    const [toggling, setToggling] = useState(new Set());
    const togglePending = useRef(new Set());
    const [form, setForm] = useState({ name: '', url: '', scope: '' });
    const [diff, setDiff] = useState(null);
    const mounted=useRef(false), addPending=useRef(false), diffRequest=useRef(0);
    const inputId=useId(), dialogRef=useRef(null), diffTrigger=useRef(null);
    const diffOpen=Boolean(diff);
    useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;diffRequest.current++;};},[]);
    const closeDiff=()=>{diffRequest.current++;setDiff(null);};
    const keepDiffFocus=event=>{
        if(event.key!=='Tab')return;
        const controls=[...event.currentTarget.querySelectorAll('button:not([disabled]), a[href]')];
        const first=controls[0],last=controls[controls.length-1];
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
    };
    // 공통 설정 도우미와 같은 native dialog로 포커스 가두기와 Escape 닫기를 유지한다.
    useEffect(()=>{
        if(!diffOpen)return;
        const dialog=dialogRef.current,trigger=diffTrigger.current;
        dialog.showModal();
        return()=>{if(dialog.open)dialog.close();if(trigger?.isConnected)trigger.focus();};
    },[diffOpen]);

    const { data: items = [], isLoading, isError, refetch } = useQuery({
        queryKey: ['policyWatch'],
        queryFn: welfareApi.getPolicyWatch,
        refetchInterval: 60000,
    });

    const reload = () => queryClient.invalidateQueries({ queryKey: ['policyWatch'] });

    const handleCheck = async () => {
        setChecking(true);
        const t = toast.loading('정책 페이지 점검 중... (변경 여부 확인)');
        try {
            const r = await welfareApi.runPolicyWatchCheck();
            const outcome = policyWatchOutcome(r);
            toast[outcome.level](outcome.message, { id: t });
            reload();
        } catch (e) {
            const msg = e?.response?.status === 409 ? '이미 점검이 진행 중입니다' : getErrorMessage(e);
            toast.error('점검 실패: ' + msg, { id: t });
        } finally {
            setChecking(false);
        }
    };

    const handleAdd = async () => {
        if(addPending.current)return;
        if (!form.url.trim()) { toast.error('URL을 입력하세요'); return; }
        const submitted={...form};
        addPending.current=true;
        setAdding(true);
        try {
            await welfareApi.addPolicyWatch(submitted);
            if(!mounted.current)return;
            toast.success('확인할 웹페이지를 추가했습니다');
            setForm(current=>Object.keys(submitted).every(key=>current[key]===submitted[key])?{name:'',url:'',scope:''}:current);
            reload();
        } catch (e) {
            if(mounted.current)toast.error('추가 실패: ' + getErrorMessage(e));
        } finally {
            addPending.current=false;
            if(mounted.current)setAdding(false);
        }
    };

    const handleDelete = async (id) => {
        if (!window.confirm('확인할 웹페이지 목록에서 삭제할까요? 원래 웹페이지는 삭제되지 않습니다.')) return;
        try { await welfareApi.deletePolicyWatch(id); toast.success('삭제됨'); reload(); }
        catch (e) { toast.error('삭제 실패'); }
    };

    const handleToggle = async (item) => {
        if (togglePending.current.has(item.id)) return;
        togglePending.current.add(item.id);
        setToggling(new Set(togglePending.current));
        try { await welfareApi.togglePolicyWatch(item.id, !item.enabled); await reload(); }
        catch (e) { toast.error('변경 실패'); }
        finally {
            togglePending.current.delete(item.id);
            if (mounted.current) setToggling(new Set(togglePending.current));
        }
    };

    const showDiff = async (id, trigger) => {
        const request=++diffRequest.current;
        diffTrigger.current=trigger;
        try { const result=await welfareApi.getPolicyWatchDiff(id);if(mounted.current&&request===diffRequest.current)setDiff(result); }
        catch (e) { if(mounted.current&&request===diffRequest.current)toast.error('변경 내용을 불러오지 못했습니다'); }
    };

    const changedCount = items.filter(i => i.last_changed_at).length;

    return (
        <div className="space-y-6 max-w-5xl mx-auto">
            <div className="flex flex-wrap gap-3 justify-between items-start">
                <div>
                    <Heading className={`${embedded ? 'ui-section-title' : 'ui-page-title'} flex items-center gap-2`}>
                        웹페이지 변경 확인
                    </Heading>
                    <p className="text-sm text-gray-500 mt-1">
                        등록한 <b>정책 페이지의 변경점만 집중 감지</b>합니다. AI 분석 없이 빠르게 확인 → 바뀐 정책은 Notion에서 직접 수정하세요.
                    </p>
                </div>
                <button
                    onClick={handleCheck}
                    disabled={checking}
                    className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2 whitespace-nowrap"
                >
                    {checking ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                    지금 점검
                </button>
            </div>

            {/* Add form */}
            <div className="bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 p-4">
                <div className="flex flex-wrap gap-2 items-end">
                    <div className="flex-1 min-w-[140px]">
                        <label htmlFor={`${inputId}-name`} className="text-xs text-gray-500">이름</label>
                        <input id={`${inputId}-name`} value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                            placeholder="예: 양육수당 안내" className="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm" />
                    </div>
                    <div className="flex-[2] min-w-[220px]">
                        <label htmlFor={`${inputId}-url`} className="text-xs text-gray-500">정책 페이지 URL</label>
                        <input id={`${inputId}-url`} value={form.url} onChange={e => setForm(f => ({ ...f, url: e.target.value }))}
                            placeholder="https://..." className="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm" />
                    </div>
                    <div className="w-36">
                        <label htmlFor={`${inputId}-scope`} className="text-xs text-gray-500">범위(태그)</label>
                        <input id={`${inputId}-scope`} value={form.scope} onChange={e => setForm(f => ({ ...f, scope: e.target.value }))}
                            placeholder="예: 영유아" className="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm" />
                    </div>
                    <button onClick={handleAdd} disabled={adding}
                        className="px-4 py-2 bg-gray-800 dark:bg-gray-700 text-white rounded-lg hover:bg-gray-900 disabled:opacity-50 flex items-center gap-1 text-sm">
                        {adding ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} 추가
                    </button>
                </div>
            </div>

            {/* Summary */}
            {changedCount > 0 && (
                <div className="bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-900/40 rounded-lg p-3 text-sm text-blue-700 dark:text-blue-300 flex items-center gap-2">
                    <Bell size={15} /> 변경 이력이 있는 정책 {changedCount}건 — 아래에서 [변경 내용]을 확인하고 Notion을 갱신하세요.
                </div>
            )}

            {/* List */}
            <div className="bg-white dark:bg-[#1e2128] rounded-xl border border-gray-100 dark:border-gray-700 overflow-hidden">
                {isError ? (
                    <p role="alert" className="p-4 text-red-700">웹페이지 목록을 불러오지 못했습니다. <button onClick={() => refetch()} className="underline">다시 불러오기</button></p>
                ) : isLoading ? (
                    <div className="p-6 text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 로딩 중...</div>
                ) : items.length === 0 ? (
                    <div className="p-8 text-center text-gray-400">확인할 웹페이지가 없습니다. 위에서 주소를 추가하세요.</div>
                ) : (
                    <table className="w-full text-sm">
                        <thead className="text-xs text-gray-500 uppercase bg-gray-50 dark:bg-gray-800">
                            <tr>
                                <th className="px-4 py-3 text-left">정책 / 범위</th>
                                <th className="px-4 py-3 text-left">마지막 점검</th>
                                <th className="px-4 py-3 text-left">마지막 변경</th>
                                <th className="px-4 py-3 text-center">변경</th>
                                <th className="px-4 py-3 text-right">관리</th>
                            </tr>
                        </thead>
                        <tbody>
                            {items.map(it => (
                                <tr key={it.id} className={`border-b dark:border-gray-700 ${!it.enabled ? 'opacity-50' : ''}`}>
                                    <td className="px-4 py-3">
                                        <div className="font-medium text-gray-800 dark:text-gray-100 flex items-center gap-1">
                                            {it.name}
                                            <a href={it.url} target="_blank" rel="noopener noreferrer" className="text-gray-400 hover:text-blue-500"><ExternalLink size={12} /></a>
                                        </div>
                                        {it.scope && <span className="text-[11px] text-blue-500">{it.scope}</span>}
                                    </td>
                                    <td className="px-4 py-3 text-gray-500 text-xs">{fmt(it.last_checked)}</td>
                                    <td className="px-4 py-3 text-xs">
                                        {it.last_changed_at
                                            ? <span className="text-blue-600 dark:text-blue-400 font-medium">{fmt(it.last_changed_at)}</span>
                                            : <span className="text-gray-400">변경 없음</span>}
                                    </td>
                                    <td className="px-4 py-3 text-center">
                                        {it.change_count > 0
                                            ? <span className="text-xs font-bold bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 px-2 py-0.5 rounded-full">{it.change_count}회</span>
                                            : <span className="text-gray-300">-</span>}
                                    </td>
                                    <td className="px-4 py-3 text-right">
                                        <div className="flex justify-end gap-1">
                                            {it.last_changed_at && (
                                                <button onClick={event => showDiff(it.id,event.currentTarget)} title="변경 내용 보기"
                                                    className="p-1.5 text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-950/30 rounded-lg"><Eye size={15} /></button>
                                            )}
                                            <button type="button" role="switch" aria-checked={Boolean(it.enabled)} aria-label={`${it.name} 변경 확인`} disabled={toggling.has(it.id)} onClick={() => handleToggle(it)}
                                                className="inline-flex min-h-11 items-center rounded-full px-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:opacity-50">
                                                <span className={`relative inline-flex h-7 w-16 items-center rounded-full text-[10px] font-semibold ${it.enabled ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-600 dark:bg-gray-700 dark:text-gray-200'}`}>
                                                    <span aria-hidden="true" className={it.enabled ? 'pl-2' : 'ml-auto pr-2'}>{it.enabled ? 'ON' : 'OFF'}</span>
                                                    <span aria-hidden="true" className={`absolute left-1 h-5 w-5 rounded-full bg-white shadow-sm transition-transform motion-reduce:transition-none ${it.enabled ? 'translate-x-9' : 'translate-x-0'}`} />
                                                </span>
                                            </button>
                                            <button onClick={() => handleDelete(it.id)} title="삭제"
                                                className="p-1.5 text-red-500 hover:bg-red-50 dark:hover:bg-red-950/30 rounded-lg"><Trash2 size={15} /></button>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>

            {/* Diff modal */}
            {diff && (
                <dialog ref={dialogRef} aria-modal="true" aria-labelledby={`${inputId}-diff-title`} onKeyDown={keepDiffFocus} onCancel={event=>{event.preventDefault();closeDiff();}}
                    onClick={event=>{if(event.target===event.currentTarget)closeDiff();}}
                    className="m-auto w-[calc(100%-2rem)] max-w-3xl max-h-[80vh] rounded-xl border-0 bg-white p-0 text-gray-800 backdrop:bg-black/40 dark:bg-[#1e2128] dark:text-gray-100">
                    <div className="max-h-[80vh] overflow-hidden flex flex-col">
                        <div className="p-4 border-b dark:border-gray-700 flex justify-between items-center">
                            <h3 id={`${inputId}-diff-title`} className="font-semibold text-gray-800 dark:text-gray-100">{diff.name} — 변경 내용</h3>
                            <button autoFocus aria-label="변경 내용 닫기" onClick={closeDiff}><X className="w-5 h-5 text-gray-400" /></button>
                        </div>
                        <div className="p-4 overflow-y-auto grid grid-cols-2 gap-4 text-xs">
                            <div>
                                <p className="font-semibold text-gray-500 mb-1">이전</p>
                                <div className="whitespace-pre-wrap bg-red-50/50 dark:bg-red-950/10 p-3 rounded-lg border border-red-100 dark:border-red-900/30">{diff.prev_text || '(없음)'}</div>
                            </div>
                            <div>
                                <p className="font-semibold text-gray-500 mb-1">현재</p>
                                <div className="whitespace-pre-wrap bg-green-50/50 dark:bg-green-950/10 p-3 rounded-lg border border-green-100 dark:border-green-900/30">{diff.last_text || '(없음)'}</div>
                            </div>
                        </div>
                        <div className="p-3 border-t dark:border-gray-700 text-right">
                            <a href={diff.url} target="_blank" rel="noopener noreferrer" className="text-sm text-blue-600 hover:underline">원본 페이지 열기 ↗</a>
                        </div>
                    </div>
                </dialog>
            )}
        </div>
    );
};

export default PolicyWatch;
