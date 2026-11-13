import React, {useEffect, useRef, useState} from 'react';
import {Loader2, Save} from 'lucide-react';
import {toast} from 'sonner';
import {welfareApi} from '../api/welfare';
import {useSettingsDraft} from '../hooks/useSettingsDraft';
import {settingsDrafts} from '../utils/settingsDrafts';
import {getErrorMessage} from '../utils/error';
import {newsRetentionDays, newsRetentionConfirmation} from '../utils/newsRetention';

export default function NewsRecordRetentionSettings() {
    const [days, setDays] = useSettingsDraft('newsRecordRetentionDays', 0);
    const [loaded, setLoaded] = useState(false), [error, setError] = useState('');
    const [saving, setSaving] = useState(false), [reload, setReload] = useState(0);
    const pending = useRef(false);
    useEffect(() => {
        let active = true;
        const request = settingsDrafts.capture();
        setLoaded(false); setError('');
        welfareApi.getNewsRecordRetention().then(result => {
            const saved = newsRetentionDays(result.days);
            if (!active) return;
            settingsDrafts.hydrate({newsRecordRetentionDays: saved}, request);
            setLoaded(true);
        }).catch(reason => {if (active) setError(getErrorMessage(reason));});
        return () => {active = false;};
    }, [reload]);
    const save = async () => {
        if (!loaded || pending.current) return;
        const submitted = days;
        pending.current = true;
        try {
            const question = newsRetentionConfirmation(submitted);
            if (question && !window.confirm(question)) return;
            setSaving(true);
            const result = await settingsDrafts.runWrite(() => welfareApi.saveNewsRecordRetention(submitted));
            const saved = newsRetentionDays(result.days);
            settingsDrafts.commit('newsRecordRetentionDays', submitted, saved);
            if (result.cleanup_failed) toast.warning('기간은 저장했지만 기록 정리는 완료하지 못했습니다. 나중에 다시 저장해주세요.');
            else if (!saved) toast.success('새 소식 기록 자동 정리를 껐습니다.');
            else if (Number.isInteger(result.cleanup?.deleted_records)) toast.success(`${saved}일 보관으로 설정했습니다. 오래된 미채택 기록 ${result.cleanup.deleted_records}건을 정리했습니다.`);
            else toast.success(`${saved}일 보관으로 설정했습니다. 정리 대상 기록만 삭제합니다.`);
        } catch (reason) {toast.error('보관 기간 저장 실패: ' + getErrorMessage(reason));}
        finally {pending.current = false; setSaving(false);}
    };
    return <section className="border-t border-gray-100 pt-5 dark:border-gray-700" aria-labelledby="news-record-retention-title">
        <h4 id="news-record-retention-title" className="font-medium text-gray-800 dark:text-gray-200">새 소식 처리 기록 보관</h4>
        <p id="news-record-retention-help" className="mt-1 text-xs leading-5 text-gray-500">미채택 수집 기록만 정리합니다. 찾은 소식·저장 자료·Notion은 유지됩니다.</p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
            <select aria-label="새 소식 기록 보관 기간" aria-describedby="news-record-retention-help" value={days}
                onChange={event => setDays(Number(event.target.value))} disabled={!loaded || saving}
                className="min-h-10 rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100">
                <option value={0}>자동 정리 안 함</option><option value={7}>7일 보관</option><option value={30}>30일 보관</option>
            </select>
            <button type="button" onClick={save} disabled={!loaded || saving}
                className="flex min-h-10 items-center gap-2 rounded-lg bg-gray-800 px-4 py-2 text-sm text-white hover:bg-gray-900 disabled:opacity-50 dark:bg-gray-700">
                {saving ? <Loader2 className="h-4 w-4 animate-spin"/> : <Save className="h-4 w-4"/>}기록 보관 기간 저장
            </button>
        </div>
        {!loaded && !error && <p role="status" className="mt-2 text-xs text-gray-500">보관 설정 확인 중…</p>}
        {error && <p role="alert" className="mt-2 text-xs text-amber-700">설정을 확인하지 못했습니다. <button type="button" onClick={() => setReload(value => value + 1)} className="underline">다시 확인</button></p>}
    </section>;
}
