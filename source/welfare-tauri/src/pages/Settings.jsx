import React, { lazy, Suspense, useState, useEffect, useRef } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { welfareApi } from '../api/welfare';
import { toast } from 'sonner';
import { getErrorMessage } from '../utils/error';
import { summarizeBackfillOutcome, nextBackfillCursor } from '../utils/embeddingBackfill';
import { formatStoredUtcTime } from '../utils/dateTime';
import { isConfirmedOsTaskRemoval, normalizeOsTaskState } from '../utils/osTaskState';
import { Save, Key, ShieldCheck, Tag, X, Loader2, Sparkles, Trash2, Activity, AlertTriangle, CheckCircle, FileText, Download, Upload, Database, Eye, EyeOff, Clock, Bell, MessageSquare, Cpu, RefreshCw, UploadCloud, ChevronDown, ChevronUp, Globe, Plus } from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { applyVerifiedPolicyTitles, createPolicyDbDraft, parsePolicyDbs, policyDbPayload } from '../constants/policyDbs';
import { NOTION_NEWS_TEMPLATE_URL } from '../constants/onboardingLinks';
import { useAiStatus } from '../hooks/useAiStatus';
import NewsProfilesPanel from '../components/NewsProfilesPanel';
import DefaultNewsConditionEditor from '../components/DefaultNewsConditionEditor';
import NewsRecordRetentionSettings from '../components/NewsRecordRetentionSettings';
import ExternalActionLink from '../components/onboarding/ExternalActionLink';
import { PaidApiHelp } from '../components/AiUsageStatus';
import { Tooltip } from '../components/ui/Tooltip';
import { useSettingsDraft, useSettingsDirtyNames } from '../hooks/useSettingsDraft';
import { settingsDrafts } from '../utils/settingsDrafts';

const Tutorial = lazy(() => import('./Tutorial'));
const saveDraftConfig = payload => settingsDrafts.runWrite(() => welfareApi.saveConfig(payload));
const DRAFT_LABELS = {
    apiKey:'AI 연결', quotaObservationDraft:'프로젝트 AI 한도',
    discordUrl:'알림 연결', gchatUrl:'알림 연결', notionToken:'Notion 연결', notionDbId:'새 소식 보관함',
    policyDbs:'정책표 연결', styleGuide:'정책 작성 스타일', schedule:'수집 예약', policyWatchSchedule:'정책 자동 감시',
    retentionDays:'자료 보존기간', newsRecordRetentionDays:'새 소식 기록 보관', defaultNewsConditionDraft:'수집 조건', targetRegion:'이전 수집 범위', targetAgeMonths:'이전 수집 범위',
};

const Settings = () => {
    const { data: aiStatus } = useAiStatus();
    const [searchParams, setSearchParams] = useSearchParams();
    const requestedTab = searchParams.get('tab');
    const normalizedRequestedTab = requestedTab === 'logs' ? 'system' : ['general', 'notion'].includes(requestedTab) ? 'connections' : requestedTab;
    const validTabs = ['connections', 'keywords', 'automation', 'system', 'guide'];
    const [activeTab, setActiveTab] = useState(validTabs.includes(normalizedRequestedTab) ? normalizedRequestedTab : 'connections');
    const [conditionsOpen, setConditionsOpen] = useState(false);
    const [conditionsLoaded, setConditionsLoaded] = useState(false);
    const selectTab = tab => {
        if (!validTabs.includes(tab)) return;
        setActiveTab(tab);
        setSearchParams(previous => {
            const next = new URLSearchParams(previous);
            next.set('tab', tab);
            return next;
        }, { replace: true });
    };
    const [apiKey, setApiKey] = useSettingsDraft('apiKey', '');
    const [showApiKey, setShowApiKey] = useState(false);
    const [isConfigured, setIsConfigured] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [googleVerify, setGoogleVerify] = useState(null);
    const [googleVerifying, setGoogleVerifying] = useState(false);

    // System state
    const [healthStats, setHealthStats] = useState([]);
    const [healthLoading, setHealthLoading] = useState(false);
    const [cleanupLoading, setCleanupLoading] = useState(false);
    const [archiveLoading, setArchiveLoading] = useState(false);

    // Notification webhooks
    const [discordUrl, setDiscordUrl] = useSettingsDraft('discordUrl', '');
    const [gchatUrl, setGchatUrl] = useSettingsDraft('gchatUrl', '');
    const [discordConfigured, setDiscordConfigured] = useState(false);
    const [gchatConfigured, setGchatConfigured] = useState(false);
    const [webhookSaving, setWebhookSaving] = useState(false);

    // Notion 연동
    const [notionToken, setNotionToken] = useSettingsDraft('notionToken', '');
    const [notionDbId, setNotionDbId] = useSettingsDraft('notionDbId', '');
    const [notionConfigured, setNotionConfigured] = useState(false);
    const [notionTokenConfigured, setNotionTokenConfigured] = useState(false);
    const [notionSaving, setNotionSaving] = useState(false);

    // 정책 최신화 대상 보관함 — 개수와 이름은 Notion을 기준으로 유동적으로 관리한다.
    const [policyDbs, setPolicyDbs] = useSettingsDraft('policyDbs', () => [createPolicyDbDraft(0)]);
    const [policySaving, setPolicySaving] = useState(false);
    const [policyVerifying, setPolicyVerifying] = useState(false);
    const [policyVerify, setPolicyVerify] = useState(null);
    const [policyDbStatuses, setPolicyDbStatuses] = useState({});
    const [disconnectedPolicyData, setDisconnectedPolicyData] = useState({ count: 0, policy_count: 0, items: [] });
    const [disconnectedCleanupLoading, setDisconnectedCleanupLoading] = useState(false);

    // (T2) 정책 작성 스타일 가이드
    const [styleGuide, setStyleGuide] = useSettingsDraft('styleGuide', '');
    const [styleGuideSaving, setStyleGuideSaving] = useState(false);
    const [styleGuideGenerating, setStyleGuideGenerating] = useState(false);

    // (U2) 정책 최신화 무료 자동 감시 (Stage1만, AI 없음)
    const [policyWatchSchedule, setPolicyWatchSchedule] = useSettingsDraft('policyWatchSchedule', { enabled: false, day_of_week: 'mon', time: '09:00', next_run: null });
    const [policyWatchSaving, setPolicyWatchSaving] = useState(false);

    // Schedule
    const [schedule, setSchedule] = useSettingsDraft('schedule', { enabled: false, mode: 'daily', time: '09:00', interval_hours: 6, headless: true, next_run: null });
    const [scheduleSaving, setScheduleSaving] = useState(false);
    // OS-level task (앱 종료 중에도 동작)
    const [osTask, setOsTask] = useState({ supported: null, registered: null });
    const [osTaskSaving, setOsTaskSaving] = useState(false);
    const [osTaskReading, setOsTaskReading] = useState(false);
    const osTaskPending = useRef(false);
    const osTaskRequest = useRef(0);

    // Embeddings
    const [embStatus, setEmbStatus] = useState(null);
    const [backfillLoading, setBackfillLoading] = useState(false);
    const backfillCancelRef = useRef(false);

    // Data retention
    const [retentionDays, setRetentionDays] = useSettingsDraft('retentionDays', 365);

    // 수집 대상 지역/연령 (다른 지자체 적용 시 이 값만 바꾸면 됨)
    const [retentionSaving, setRetentionSaving] = useState(false);


    // 시스템 탭 — 자주 안 쓰는 1회성 도구 접이식 섹션
    const [advancedOpen, setAdvancedOpen] = useState(false);
    const [systemHealthOpen, setSystemHealthOpen] = useState(false);
    const [systemAdvancedOpen, setSystemAdvancedOpen] = useState(requestedTab === 'logs');

    // 실시간 로그 (구 대시보드 SYSTEM LOGS 이동분) — 시스템 탭 열려 있을 때만 폴링
    const [liveLogs, setLiveLogs] = useState([]);

    const baseSettingsLoaded = useRef(false);
    const dirtyNames = useSettingsDirtyNames().filter(name => Object.hasOwn(DRAFT_LABELS, name));
    const [discardRequest, setDiscardRequest] = useState(null);

    // 사용 가이드에서는 자체 쿼리만 사용한다. 실제 설정 탭에 처음 들어갈 때만 무거운 설정 목록을 읽는다.
    useEffect(() => {
        if (activeTab === 'guide' || baseSettingsLoaded.current) return;
        baseSettingsLoaded.current = true;
        loadConfig();
    }, [activeTab]);

    useEffect(() => {
        if (validTabs.includes(normalizedRequestedTab)) setActiveTab(normalizedRequestedTab);
        if (requestedTab === 'logs') setSystemAdvancedOpen(true);
    }, [normalizedRequestedTab, requestedTab]);

    // 시스템 탭 진입 시에만 Health 데이터 로드
    useEffect(() => {
        if (activeTab === 'automation') { loadSchedule(); }
        if (activeTab === 'automation') { loadPolicyWatchSchedule(); }
    }, [activeTab]);

    useEffect(() => {
        if (activeTab !== 'system' || !systemHealthOpen) return;
        loadHealth();
    }, [activeTab, systemHealthOpen]);

    // 의미 검색·복지로 상태는 고급 시스템 관리가 실제로 열렸을 때만 읽는다.
    useEffect(() => {
        if (activeTab !== 'system' || !systemAdvancedOpen) return;
        loadEmbStatus();
    }, [activeTab, systemAdvancedOpen]);

    // 실시간 로그 — 시스템 탭이 열려 있는 동안만 폴링(다른 탭에선 불필요한 요청 방지)
    useEffect(() => {
        if (activeTab !== 'system' || !systemAdvancedOpen) return;
        const load = () => welfareApi.getLogs().then(setLiveLogs).catch(() => {});
        load();
        const t = setInterval(load, 10000);
        return () => clearInterval(t);
    }, [activeTab, systemAdvancedOpen]);

    const loadConfig = async () => {
        const request = settingsDrafts.capture();
        try {
            const [config, dbState, disconnected] = await Promise.all([
                welfareApi.getConfig(),
                welfareApi.getPolicyDbs().catch(() => ({ dbs: [] })),
                welfareApi.getDisconnectedPolicyData().catch(() => ({ count: 0, policy_count: 0, items: [] })),
            ]);
            // Server masks are not editable keys. Both setup surfaces use a blank secret input.
            setIsConfigured(Boolean(config.is_configured));
            setDiscordConfigured(!!config.discord_configured);
            setGchatConfigured(!!config.google_chat_configured);
            if (config.DATA_RETENTION_DAYS !== undefined) {
                settingsDrafts.hydrate({retentionDays: config.DATA_RETENTION_DAYS}, request);
            }
            setNotionConfigured(!!config.notion_configured);
            setNotionTokenConfigured(!!config.notion_token_configured);
            if (config.NOTION_DATABASE_ID !== undefined) {
                settingsDrafts.hydrate({notionDbId: config.NOTION_DATABASE_ID || ''}, request);
            }
            const savedPolicyDbs = parsePolicyDbs(config.NOTION_POLICY_DBS);
            settingsDrafts.hydrate({policyDbs: savedPolicyDbs.length ? savedPolicyDbs : [createPolicyDbDraft(0)]}, request);
            setPolicyDbStatuses(Object.fromEntries((dbState.dbs || []).map(db => [db.key, db])));
            setDisconnectedPolicyData(disconnected);
            if (config.POLICY_STYLE_GUIDE !== undefined) {
                settingsDrafts.hydrate({styleGuide: config.POLICY_STYLE_GUIDE || ''}, request);
            }
        } catch (error) {
            console.error("Failed to load config:", error);
        }
    };

    const handleSaveNotion = async () => {
        const payload = {};
        if (notionToken.trim()) payload.NOTION_TOKEN = notionToken.trim();
        if (notionDbId.trim()) payload.NOTION_DATABASE_ID = notionDbId.trim();
        if (Object.keys(payload).length === 0) {
            toast.info('Notion 연결 토큰 또는 새 소식 보관함 주소를 입력하세요.');
            return;
        }
        setNotionSaving(true);
        let saved = false;
        try {
            await saveDraftConfig(payload);
            saved = true;
            if (payload.NOTION_TOKEN) settingsDrafts.commit('notionToken', notionToken, '');
            if (payload.NOTION_DATABASE_ID) settingsDrafts.commit('notionDbId', notionDbId, payload.NOTION_DATABASE_ID);
            await loadConfig();
            if (notionDbId.trim() || notionConfigured) {
                const r = await welfareApi.verifyNotion();
                if (r.ok) toast.success(`새 소식 DB 연결 성공 — "${r.title}"`);
                else toast.warning(`토큰은 저장됨. 새 소식 DB 확인 필요: ${r.error}`);
            } else {
                toast.success('Notion 연결 토큰을 저장했습니다. 아래에서 사용할 정책표를 연결하세요.');
            }
        } catch (error) {
            if (saved) toast.warning('설정은 저장했습니다. 연결 확인을 완료하지 못했습니다: ' + getErrorMessage(error));
            else toast.error('Notion 설정 저장 실패: ' + getErrorMessage(error));
        } finally {
            setNotionSaving(false);
        }
    };


    const handleSavePolicyDbs = async () => {
        const arr = policyDbPayload(policyDbs);
        setPolicySaving(true);
        try {
            await saveDraftConfig({ NOTION_POLICY_DBS: JSON.stringify(arr) });
            settingsDrafts.commit('policyDbs', policyDbs, arr.length ? arr : [createPolicyDbDraft(0)]);
            toast.success(arr.length ? `정책표 ${arr.length}개를 저장했습니다.` : '정책표 연결을 모두 해제했습니다. Notion 원본은 그대로 유지됩니다.');
            await loadConfig();
            if (arr.length) await handleVerifyPolicyDbs();
            else setPolicyVerify(null);
        } catch (error) {
            toast.error('저장 실패: ' + getErrorMessage(error));
        } finally {
            setPolicySaving(false);
        }
    };

    const addPolicyDb = () => {
        setPolicyDbs(rows => [...rows, createPolicyDbDraft(rows.length)]);
        setPolicyVerify(null);
    };

    const removePolicyDb = key => {
        setPolicyDbs(rows => {
            const next = rows.filter(row => row.key !== key);
            return next.length ? next : [createPolicyDbDraft(0)];
        });
        setPolicyVerify(null);
        toast.info('앱의 연결 목록에서 뺐습니다. 아래 저장 버튼을 눌러 반영하세요. Notion 원본은 삭제되지 않습니다.');
    };

    const handleVerifyPolicyDbs = async () => {
        const before = settingsDrafts.get('policyDbs');
        const request = settingsDrafts.capture();
        setPolicyVerifying(true);
        try {
            const result = await welfareApi.verifyPolicyDbs();
            setPolicyVerify(result);
            if (result.items?.length && !settingsDrafts.dirty('policyDbs') && request.policyDbs === settingsDrafts.capture().policyDbs) {
                const currentRows = policyDbPayload(before);
                const titled = applyVerifiedPolicyTitles(currentRows, result.items);
                if (JSON.stringify(titled) !== JSON.stringify(currentRows)) {
                    await saveDraftConfig({ NOTION_POLICY_DBS: JSON.stringify(titled) });
                    settingsDrafts.commit('policyDbs', before, titled.length ? titled : [createPolicyDbDraft(0)]);
                }
            }
            const dbState = await welfareApi.getPolicyDbs().catch(() => ({ dbs: [] }));
            setPolicyDbStatuses(Object.fromEntries((dbState.dbs || []).map(db => [db.key, db])));
            if (result.ok) toast.success('등록한 정책표의 연결과 항목 구성을 확인했습니다.');
            else toast.warning('확인이 필요한 정책표가 있습니다. 정책표별 안내를 확인하세요.');
            return result;
        } catch (error) {
            toast.error('정책표 연결을 확인하지 못했습니다. 연결 토큰과 주소를 다시 확인해주세요: ' + getErrorMessage(error));
            return null;
        } finally {
            setPolicyVerifying(false);
        }
    };

    const handleCleanupDisconnectedPolicyData = async () => {
        const count = Number(disconnectedPolicyData.policy_count || 0);
        const confirmed = window.confirm(
            `연결 해제된 보관함의 로컬 정책 ${count}건과 임시 설정을 정리할까요?\n\n` +
            'Notion 원본은 삭제되지 않으며, 정리 전 로컬 데이터베이스 백업을 만듭니다.'
        );
        if (!confirmed) return;
        setDisconnectedCleanupLoading(true);
        try {
            const result = await welfareApi.cleanupDisconnectedPolicyData();
            toast.success(`연결 해제 자료를 정리했습니다. 로컬 정책 ${result.deleted || 0}건 삭제 · Notion 원본 유지`);
            setDisconnectedPolicyData({ count: 0, policy_count: 0, items: [] });
        } catch (error) {
            toast.error('연결 해제 자료를 정리하지 못했습니다: ' + getErrorMessage(error));
        } finally {
            setDisconnectedCleanupLoading(false);
        }
    };

    const handleGenerateStyleGuide = async () => {
        setStyleGuideGenerating(true);
        const t = toast.loading('기존 작성 내용을 분석해 스타일 가이드 생성 중...');
        try {
            const r = await welfareApi.generatePolicyStyleGuide([]);
            setStyleGuide(r.guide || '');
            toast.success(`${r.based_on}건 분석 완료 — 아래 내용을 확인 후 저장하세요`, { id: t });
        } catch (error) {
            toast.error('생성 실패: ' + getErrorMessage(error), { id: t });
        } finally {
            setStyleGuideGenerating(false);
        }
    };

    const handleSaveStyleGuide = async () => {
        setStyleGuideSaving(true);
        try {
            await saveDraftConfig({ POLICY_STYLE_GUIDE: styleGuide });
            settingsDrafts.commit('styleGuide', styleGuide);
            toast.success('스타일 가이드가 저장되었습니다.');
        } catch (error) {
            toast.error('저장 실패: ' + getErrorMessage(error));
        } finally {
            setStyleGuideSaving(false);
        }
    };


    const handleSaveRetention = async () => {
        const n = Number(retentionDays);
        if (!Number.isInteger(n) || n < 0 || n > 3650) {
            toast.error('보존기간은 0~3650일 사이 정수여야 합니다.');
            return;
        }
        setRetentionSaving(true);
        try {
            await saveDraftConfig({ DATA_RETENTION_DAYS: String(n) });
            settingsDrafts.commit('retentionDays', retentionDays, n);
            toast.success(n === 0 ? '자동 정리가 비활성화되었습니다.' : `보존기간이 ${n}일로 저장되었습니다.`);
        } catch (error) {
            toast.error('저장 실패: ' + getErrorMessage(error));
        } finally {
            setRetentionSaving(false);
        }
    };


    // --- Notification Webhooks ---
    const handleSaveWebhooks = async () => {
        const payload = {};
        if (discordUrl.trim()) payload.DISCORD_WEBHOOK_URL = discordUrl.trim();
        if (gchatUrl.trim()) payload.GOOGLE_CHAT_WEBHOOK_URL = gchatUrl.trim();
        if (Object.keys(payload).length === 0) {
            toast.info('변경할 알림 연결 주소를 입력하세요.');
            return;
        }
        setWebhookSaving(true);
        try {
            await saveDraftConfig(payload);
            toast.success('알림 연결 주소를 저장했습니다.');
            if (payload.DISCORD_WEBHOOK_URL) settingsDrafts.commit('discordUrl', discordUrl, '');
            if (payload.GOOGLE_CHAT_WEBHOOK_URL) settingsDrafts.commit('gchatUrl', gchatUrl, '');
            await loadConfig();
        } catch (error) {
            toast.error('알림 연결 주소를 저장하지 못했습니다. 주소를 확인한 뒤 다시 시도해주세요: ' + getErrorMessage(error));
        } finally {
            setWebhookSaving(false);
        }
    };

    // --- Schedule ---
    const loadSchedule = async () => {
        const request = settingsDrafts.capture();
        try {
            const s = await welfareApi.getSchedule();
            settingsDrafts.hydrate({schedule: {...schedule, ...s}}, request);
        } catch (error) {
            console.error('Failed to load schedule:', error);
        }
        await loadOsTask();
    };

    const loadOsTask = async () => {
        const request=++osTaskRequest.current;
        setOsTaskReading(true);
        try {
            const result=normalizeOsTaskState(await welfareApi.getOsTask());
            if(request===osTaskRequest.current)setOsTask(result);
        } catch {
            if(request===osTaskRequest.current)setOsTask(prev=>({...prev,registered:null,error:'현재 등록 상태를 확인하지 못했습니다. 다시 확인해주세요.'}));
        } finally {
            if(request===osTaskRequest.current)setOsTaskReading(false);
        }
    };

    const loadPolicyWatchSchedule = async () => {
        const request = settingsDrafts.capture();
        try {
            const s = await welfareApi.getPolicyWatchSchedule();
            settingsDrafts.hydrate({policyWatchSchedule: {...policyWatchSchedule, ...s}}, request);
        } catch (error) {
            console.error('Failed to load policy watch schedule:', error);
        }
    };

    const handleSavePolicyWatchSchedule = async (next) => {
        setPolicyWatchSaving(true);
        try {
            const r = await settingsDrafts.runWrite(() => welfareApi.savePolicyWatchSchedule({
                enabled: next.enabled, day_of_week: next.day_of_week, time: next.time,
            }));
            settingsDrafts.commit('policyWatchSchedule', policyWatchSchedule, {...next, next_run:r.next_run});
            toast.success(next.enabled ? '자동 감시가 활성화되었습니다.' : '자동 감시가 비활성화되었습니다.');
        } catch (error) {
            toast.error('저장 실패: ' + getErrorMessage(error));
        } finally {
            setPolicyWatchSaving(false);
        }
    };

    const handleToggleOsTask = async () => {
        if(osTaskPending.current||osTaskReading||typeof osTask.registered!=='boolean'||osTask.error)return;
        osTaskPending.current=true;
        setOsTaskSaving(true);
        try {
            if (osTask.registered) {
                const r = await welfareApi.deleteOsTask();
                if(isConfirmedOsTaskRemoval(r)){
                    setOsTask(prev => ({ ...prev, registered: false,error:'' }));
                    toast.success('Windows 자동 실행 작업이 해제되었습니다.');
                }else{
                    await loadOsTask();
                    toast.warning('해제 결과를 확인하지 못했습니다. 현재 등록 상태를 다시 확인해주세요.');
                }
            } else {
                const { mode, time, interval_hours } = schedule;
                await welfareApi.registerOsTask({ mode, time, interval_hours: Number(interval_hours) });
                setOsTask(prev => ({ ...prev, registered: true }));
                toast.success('Windows 자동 실행 작업이 등록되었습니다. (앱이 꺼져 있어도 실행)');
            }
        } catch (error) {
            await loadOsTask();
            toast.error('작업 변경 결과를 확인하지 못했습니다. 현재 등록 상태를 확인해주세요. ' + (error.response?.data?.detail?.message||getErrorMessage(error)));
        } finally {
            osTaskPending.current=false;
            setOsTaskSaving(false);
        }
    };

    const handleSaveSchedule = async () => {
        setScheduleSaving(true);
        try {
            const { enabled, mode, time, interval_hours, headless } = schedule;
            const res = await settingsDrafts.runWrite(() => welfareApi.saveSchedule({ enabled, mode, time, interval_hours: Number(interval_hours), headless }));
            settingsDrafts.commit('schedule', schedule, {...schedule, interval_hours:Number(interval_hours), next_run:res.next_run});
            toast.success(enabled
                ? `예약 저장됨 — 다음 실행: ${res.next_run ? new Date(res.next_run).toLocaleString('ko-KR') : '-'}`
                : '예약이 비활성화되었습니다.');
        } catch (error) {
            toast.error('예약 저장 실패: ' + getErrorMessage(error));
        } finally {
            setScheduleSaving(false);
        }
    };

    // --- Embeddings backfill ---
    const loadEmbStatus = async () => {
        try {
            const s = await welfareApi.getEmbeddingsStatus();
            setEmbStatus(s);
        } catch (error) {
            console.error('Failed to load embedding status:', error);
        }
    };

    const handleBackfill = async () => {
        setBackfillLoading(true);
        backfillCancelRef.current = false;
        try {
            let totalDone = 0;
            let guard = 0;
            let lastResult = null;
            let cancelled = false;
            let cursor = 0;
            let totalHeld = 0;
            // 배치 반복: 남은 항목이 0이 되거나, 이번 배치가 하나도 성공 못 했거나(실패로
            // 간주해 중단 — 무한 재시도 방지), 사용자가 취소할 때까지.
            while (guard < 1000) {
                if (backfillCancelRef.current) {
                    cancelled = true;
                    break;
                }
                guard += 1;
                const r = await welfareApi.backfillEmbeddings(100, cursor);
                totalDone += r.embedded;
                totalHeld += r.held_for_input_size || 0;
                lastResult = { ...r, held_for_input_size: totalHeld };
                if (r.stats) setEmbStatus(r.stats);
                const next = nextBackfillCursor(r, cursor);
                if (next === null) break;
                cursor = next;
            }
            if (cancelled) {
                toast.info(`의미 검색 데이터 생성을 중단했습니다. (이번 세션 ${totalDone}건 생성)`);
            } else if (lastResult) {
                // (2026-08-05, GPT Codex 리뷰) embedded===0을 "이미 최신"으로만 해석하면
                // 이번 배치가 전부 실패한 경우(예: 한도 소진 도중 처음 걸림)도 성공으로
                // 오인 표시된다 — Search.jsx와 동일한 판정 함수로 failed/한도 사유까지 구분.
                const outcome = summarizeBackfillOutcome(totalDone, lastResult);
                if (outcome.status === 'success') toast.success(outcome.message);
                else if (outcome.status === 'partial' || outcome.status === 'quota') toast.warning(outcome.message);
                else toast.error(outcome.message);
            }
            await loadEmbStatus();
        } catch (error) {
            toast.error('의미 검색 데이터 생성 실패: ' + getErrorMessage(error));
        } finally {
            setBackfillLoading(false);
            backfillCancelRef.current = false;
        }
    };

    const handleCancelBackfill = () => {
        backfillCancelRef.current = true;
    };

    const handleSave = async (e) => {
        e.preventDefault();
        if (!apiKey || apiKey.includes('...')) {
            toast.error('유효한 API 키를 입력해주세요.');
            return;
        }
        setIsLoading(true);
        try {
            await saveDraftConfig({ GOOGLE_API_KEY: apiKey });
            settingsDrafts.commit('apiKey', apiKey, '');
            await loadConfig();
            const verification = await handleVerifyGoogle();
            if (verification?.ok) toast.success('API 키 저장과 연결 확인을 완료했습니다.');
        } catch (error) {
            console.error("Save error:", error);
            toast.error('설정 저장 실패');
        } finally {
            setIsLoading(false);
        }
    };


    const handleVerifyGoogle = async () => {
        setGoogleVerifying(true);
        try {
            const result = await welfareApi.verifyGoogleApiKey();
            setGoogleVerify(result);
            if (!result.ok) toast.warning(result.error || 'API 키 연결을 확인하지 못했습니다.');
            return result;
        } catch (error) {
            const result = { ok: false, status: 'network_error', error: getErrorMessage(error) };
            setGoogleVerify(result);
            toast.error('API 키 확인 실패: ' + result.error);
            return result;
        } finally {
            setGoogleVerifying(false);
        }
    };


    // --- System Functions ---
    const loadHealth = async () => {
        setHealthLoading(true);
        try {
            const stats = await welfareApi.getHealth();
            setHealthStats(stats);
        } catch (error) {
            console.error('Failed to load health stats:', error);
            toast.error('시스템 상태를 불러오지 못했습니다.');
        } finally { setHealthLoading(false); }
    };

    const handleArchiveExpired = async () => {
        if (!window.confirm('마감일이 지난 항목을 아카이브 처리하시겠습니까?\n(검색에서 숨겨지며, 데이터는 보존됩니다.)')) return;
        setArchiveLoading(true);
        try {
            const result = await welfareApi.archiveExpired();
            toast.success(`${result.archived_count}개 항목이 아카이브 처리되었습니다.`);
            loadHealth();
        } catch (error) {
            toast.error('아카이브 실패: ' + getErrorMessage(error));
        } finally {
            setArchiveLoading(false);
        }
    };

    const handleCleanup = async () => {
        if (!window.confirm('90일 이상 된 데이터와 만료된 데이터를 삭제하시겠습니까? \n이 작업은 되돌릴 수 없습니다.')) return;
        setCleanupLoading(true);
        try {
            const result = await welfareApi.cleanupData(90);
            toast.success(`${result.deleted_count}개 항목이 정리되었습니다.`);
            loadHealth();
        } catch (error) {
            toast.error('데이터 정리 실패');
        } finally {
            setCleanupLoading(false);
        }
    };

    const handleBackup = async () => {
        try {
            await welfareApi.backupDb();
            toast.success('데이터 백업 파일을 다운로드했습니다.');
        } catch (error) {
            toast.error('백업 파일을 만들지 못했습니다. 잠시 후 다시 시도해주세요.');
        }
    };

    const handleRestore = async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        if (!window.confirm('선택한 백업 파일로 데이터를 복원할까요?\n\n현재 데이터는 선택한 파일의 내용으로 교체됩니다. 복원 직전 상태는 안전 백업으로 자동 보관됩니다.')) {
            e.target.value = '';
            return;
        }

        const loadingToast = toast.loading('데이터 복원 중...');
        try {
            await welfareApi.restoreDb(file);
            toast.success('백업 파일의 데이터로 복원했습니다.', { id: loadingToast });
            loadHealth();
        } catch (error) {
            toast.error('복원 실패: ' + getErrorMessage(error), { id: loadingToast });
        } finally {
            e.target.value = '';
        }
    };

    const handleSettingsTabKeyDown = (event, tab) => {
        const currentIndex = validTabs.indexOf(tab);
        let nextIndex = currentIndex;
        if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % validTabs.length;
        else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + validTabs.length) % validTabs.length;
        else if (event.key === 'Home') nextIndex = 0;
        else if (event.key === 'End') nextIndex = validTabs.length - 1;
        else return;
        event.preventDefault();
        const nextTab = validTabs[nextIndex];
        selectTab(nextTab);
        requestAnimationFrame(() => document.getElementById(`settings-tab-${nextTab}`)?.focus());
    };

    return (
        <div className="space-y-6">
            <div className="flex items-center mb-6">
                <h1 className="ui-page-title flex items-center gap-2">
                    설정
                </h1>
            </div>

            {/* Tabs */}
            {dirtyNames.length > 0 && <div role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
                <p className="font-semibold">아직 저장하지 않은 설정이 있습니다</p>
                <p>변경한 항목: {[...new Set(dirtyNames.map(name => DRAFT_LABELS[name] || '설정'))].join(' · ')}</p>
                <p>다른 화면에 다녀와도 입력은 유지됩니다. 변경한 영역의 저장 버튼을 눌러주세요. 앱을 종료하면 미저장 내용은 사라집니다.</p>
                {!discardRequest ? <button type="button" className="mt-2 underline underline-offset-2" onClick={() => setDiscardRequest(Object.fromEntries(Object.entries(settingsDrafts.capture()).filter(([name]) => Object.hasOwn(DRAFT_LABELS, name))))}>미저장 변경 취소…</button>
                    : <div className="mt-2 flex flex-wrap items-center gap-3"><span>미저장 입력을 마지막 저장 상태로 되돌릴까요?</span>
                        <button type="button" className="rounded-lg border px-3 py-1" onClick={() => setDiscardRequest(null)}>계속 수정</button>
                        <button type="button" disabled={settingsDrafts.isWriting()} className="rounded-lg border border-amber-400 px-3 py-1 font-semibold disabled:opacity-50" onClick={() => {settingsDrafts.discard(discardRequest);setDiscardRequest(null);loadConfig();}}>변경 취소</button>
                    </div>}
            </div>}
            <div className="flex flex-wrap gap-x-1 sm:gap-x-2 border-b border-gray-200 dark:border-gray-700 mb-6" role="tablist" aria-label="설정 항목">
                {validTabs.map(tab => (
                    <button
                        key={tab}
                        id={`settings-tab-${tab}`}
                        type="button"
                        onClick={() => selectTab(tab)}
                        onKeyDown={(event) => handleSettingsTabKeyDown(event, tab)}
                        role="tab"
                        aria-selected={activeTab === tab}
                        aria-controls={`settings-panel-${tab}`}
                        tabIndex={activeTab === tab ? 0 : -1}
                        className={`shrink-0 whitespace-nowrap px-3 sm:px-4 py-2 font-medium text-sm transition-colors border-b-2 ${activeTab === tab
                            ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                            : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'
                            }`}
                    >
                        {tab === 'connections' && '연결 관리'}
                        {tab === 'keywords' && '기능 설정'}
                        {tab === 'automation' && '자동화/알림'}
                        {tab === 'system' && '시스템 관리'}
                        {tab === 'guide' && '시작 안내'}
                    </button>
                ))}
            </div>

            {/* Content */}
            <AnimatePresence mode='sync'>
                {activeTab === 'guide' && (
                    <motion.div
                        key="guide"
                        id="settings-panel-guide"
                        role="tabpanel"
                        aria-labelledby="settings-tab-guide"
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                    >
                        <Suspense fallback={<div className="rounded-2xl border border-gray-200 bg-white p-6 text-sm text-gray-500">도움말과 설정 도우미를 불러오는 중입니다...</div>}>
                            <Tutorial embedded />
                        </Suspense>
                    </motion.div>
                )}
                {activeTab === 'connections' && (
                    <motion.div
                        key="connections"
                        id="settings-panel-connections"
                        role="tabpanel"
                        aria-labelledby="settings-tab-connections"
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                        className="space-y-6"
                    >
                        {/* API Key Section */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-gray-100 dark:border-gray-700">
                            <div className="flex items-center justify-between gap-3 p-6 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-[#22252b]">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <ShieldCheck className="w-5 h-5 text-blue-500" /> AI 연결 <span className="text-sm font-normal text-gray-400">(Gemini)</span>
                                </h2>
                                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${isConfigured ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'}`}>{isConfigured ? '키 저장됨' : '설정 필요'}</span>
                            </div>
                            <div className="p-6">
<div className="space-y-3">
                                    <Link to="/settings?tab=guide&step=google&edit=google" className="inline-flex rounded-xl bg-blue-600 px-4 py-3 text-sm font-semibold text-white">{isConfigured ? 'Gemini 연결 확인·변경' : 'Gemini 연결하기'}</Link>
                                </div>
                                <div className="mt-4 flex flex-wrap items-start gap-x-5 gap-y-2 text-xs leading-5"><PaidApiHelp compact />
                                <a href="https://aistudio.google.com/usage" target="_blank" rel="noopener noreferrer" className="inline-block text-xs leading-5 text-blue-600 underline">Gemini 사용량·한도 확인 ↗</a>
                                <div className="min-w-28 flex-1 text-xs leading-5 text-gray-600 dark:text-gray-300">
                                    <details>
                                        <summary className="w-fit cursor-pointer rounded text-blue-600 underline underline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 dark:text-blue-400">AI 사용량 보호</summary>
                                        <p className="mt-2">중복 요청과 과도한 재시도를 방지하고 요청 간격을 조절합니다. Google의 사용 제한으로 멈춘 작업은 보관됩니다.</p>
                                        <p className="mt-2">AI 분석은 Google Gemini를 활용합니다.</p>
                                        <p className="mt-2">분석에 필요한 문서 내용은 Google Gemini API에서 처리됩니다. 데이터 이용 기준은 Google의 서비스 조건을 따릅니다.</p>
                                        <a href="https://ai.google.dev/gemini-api/terms" target="_blank" rel="noopener noreferrer" className="mt-1 inline-block text-blue-600 underline underline-offset-2 dark:text-blue-400">Google 데이터 처리·이용 조건 ↗</a>
                                    </details>
                                </div>
                                </div>
                            </div>
                        </div>

                    </motion.div>
                )}

                {activeTab === 'keywords' && (
                    <motion.div
                        key="keywords"
                        id="settings-panel-keywords"
                        role="tabpanel"
                        aria-labelledby="settings-tab-keywords"
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                        className="max-w-4xl space-y-6"
                    >
                        <h2 className="ui-section-title">정책 최신화 설정</h2>
                        <details id="policy-writing-style" open={searchParams.get('section') === 'style' || undefined} className="ui-disclosure">
                            <summary className="sm:p-6">
                                <span className="flex flex-col items-stretch gap-4 sm:flex-row sm:items-center">
                                    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-blue-50 dark:bg-blue-950/30"><Sparkles aria-hidden="true" className="h-5 w-5 text-blue-600 dark:text-blue-400" /></span>
                                    <span className="min-w-0 flex-1">
                                        <span className="ui-section-title block">정책 작성 스타일</span>
                                        <span className="ui-description mt-1 block">AI가 제안하는 정책 문체·서식을 확인하고 수정합니다.</span>
                                    </span>
                                    <span className="inline-flex min-h-11 w-full shrink-0 items-center justify-center whitespace-nowrap rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white sm:w-40">스타일 편집</span>
                                </span>
                            </summary>
                            <div className="border-t border-gray-100 dark:border-gray-700">
                                <div className="p-5 border-b border-indigo-100 dark:border-indigo-900/30 bg-indigo-50/50 dark:bg-indigo-950/10">
                                    <h3 className="text-base font-semibold text-gray-800 dark:text-gray-100">정책 작성 스타일 가이드</h3>
                                </div>
                            <div className="p-6 space-y-4">
                                <p className="text-sm text-gray-500">
                                    기존에 Notion에 직접 작성해두신 정책 내용을 분석해, 앞으로 변경 제안을 만들 때
                                    같은 문체·서식으로 맞추는 데 씁니다. 자동 생성 후 내용을 확인하고 필요하면 직접 고쳐서 저장하세요.
                                </p>
                                <button onClick={handleGenerateStyleGuide} disabled={styleGuideGenerating || aiStatus?.busy}
                                    className="px-3 py-2 text-sm border border-indigo-300 dark:border-indigo-700 text-indigo-600 dark:text-indigo-300 rounded-lg hover:bg-indigo-50 dark:hover:bg-indigo-950/20 disabled:opacity-50 flex items-center gap-1.5">
                                    {styleGuideGenerating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                                    기존 작성 내용에서 자동 생성
                                </button>
                                <textarea value={styleGuide} onChange={(e) => setStyleGuide(e.target.value)}
                                    rows={10} placeholder="자동 생성 버튼을 누르거나 직접 작성하세요."
                                    className="w-full px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm font-mono" />
                                <button onClick={handleSaveStyleGuide} disabled={styleGuideSaving}
                                    className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2 text-sm">
                                    {styleGuideSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                    가이드 저장
                                </button>
                            </div>
                            </div>
                        </details>
                        <h2 className="ui-section-title border-t border-gray-200 pt-6 dark:border-gray-700">새 소식 설정</h2>
                        <div className="ui-card flex flex-col items-stretch gap-4 p-4 sm:flex-row sm:items-center sm:p-6">
                            <div className="w-11 h-11 rounded-xl bg-teal-50 dark:bg-teal-950/30 flex items-center justify-center flex-shrink-0">
                                <Globe aria-hidden="true" className="w-5 h-5 text-teal-700 dark:text-teal-400" />
                            </div>
                            <div className="flex-1 min-w-0">
                                <h2 className="ui-section-title">수집 사이트 등록·관리</h2>
                                <p className="text-sm text-gray-500 mt-1">수집할 기관 사이트를 등록하고 정상 수집 여부를 확인합니다.</p>
                            </div>
                            <Link to="/sites" className="inline-flex min-h-11 w-full shrink-0 items-center justify-center rounded-lg bg-teal-700 px-4 py-2 text-sm font-medium text-white hover:bg-teal-800 sm:w-40 whitespace-nowrap focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700">
                                사이트 관리 열기
                            </Link>
                        </div>
                        <section aria-labelledby="current-news-condition-title" className="ui-card p-4 text-gray-800 sm:p-6 dark:text-gray-100">
                            <h2 id="current-news-condition-title" className="ui-section-title mb-3">새 소식 수집 조건</h2>
                            <DefaultNewsConditionEditor />
                        </section>
                        <section aria-label="저장한 다른 조건" className="ui-card p-4 sm:p-6">
                            <div>
                                <button type="button" aria-expanded={conditionsOpen} aria-controls="collection-conditions" onClick={() => {setConditionsLoaded(true);setConditionsOpen(value => !value);}}
                                    className="inline-flex min-h-10 items-center gap-2 rounded-lg text-sm font-medium text-gray-600 dark:text-gray-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500">
                                    <ChevronDown aria-hidden="true" className={`h-4 w-4 transition-transform ${conditionsOpen ? 'rotate-180' : ''}`} />
                                    저장한 다른 조건
                                </button>
                                {!conditionsOpen && <p className="ui-description mt-1">전에 저장한 별도 조건과 분석 기록을 확인합니다. 평소에는 위의 수집 조건만 바꾸면 됩니다.</p>}
                                {conditionsLoaded && <div id="collection-conditions" hidden={!conditionsOpen} className="pt-3"><NewsProfilesPanel embedded /></div>}
                            </div>
                        </section>

                    </motion.div>
                )}

                {activeTab === 'automation' && (
                    <motion.div
                        key="automation"
                        id="settings-panel-automation"
                        role="tabpanel"
                        aria-labelledby="settings-tab-automation"
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                        className="space-y-6"
                    >
                        {/* Schedule */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-purple-100 dark:border-purple-900/30 overflow-hidden">
                            <div className="p-6 border-b border-purple-100 dark:border-purple-900/30 bg-purple-50/50 dark:bg-purple-950/10 flex justify-between items-center">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <Clock className="w-5 h-5 text-purple-500" /> 자동 수집 예약
                                </h2>
                                <label className="flex items-center gap-2 cursor-pointer">
                                    <span className="text-sm text-gray-600 dark:text-gray-300">{schedule.enabled ? '사용' : '사용 안 함'}</span>
                                    <input
                                        type="checkbox"
                                        checked={schedule.enabled}
                                        onChange={(e) => setSchedule(p => ({ ...p, enabled: e.target.checked }))}
                                        className="w-5 h-5 accent-purple-600"
                                    />
                                </label>
                            </div>
                            <div className="p-6 space-y-4">
                                <div className="flex flex-wrap gap-3 items-center">
                                    <span className="text-sm text-gray-500 w-20 shrink-0">실행 방식</span>
                                    <div className="flex gap-2">
                                        <button
                                            onClick={() => setSchedule(p => ({ ...p, mode: 'daily' }))}
                                            className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${schedule.mode === 'daily' ? 'bg-purple-100 border-purple-400 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300' : 'border-gray-200 text-gray-500 dark:border-gray-600'}`}
                                        >매일 지정 시각</button>
                                        <button
                                            onClick={() => setSchedule(p => ({ ...p, mode: 'interval' }))}
                                            className={`px-3 py-1.5 text-sm rounded-lg border transition-colors ${schedule.mode === 'interval' ? 'bg-purple-100 border-purple-400 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300' : 'border-gray-200 text-gray-500 dark:border-gray-600'}`}
                                        >일정 간격</button>
                                    </div>
                                </div>

                                {schedule.mode === 'daily' ? (
                                    <div className="flex items-center gap-3">
                                        <span className="text-sm text-gray-500 w-20">실행 시각</span>
                                        <input
                                            type="time"
                                            value={schedule.time}
                                            onChange={(e) => setSchedule(p => ({ ...p, time: e.target.value }))}
                                            className="px-3 py-2 border border-gray-200 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-gray-100"
                                        />
                                    </div>
                                ) : (
                                    <div className="flex items-center gap-3">
                                        <span className="text-sm text-gray-500 w-20">실행 간격</span>
                                        <input
                                            type="number"
                                            min={1}
                                            max={168}
                                            value={schedule.interval_hours}
                                            onChange={(e) => setSchedule(p => ({ ...p, interval_hours: e.target.value }))}
                                            className="w-24 px-3 py-2 border border-gray-200 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-gray-100"
                                        />
                                        <span className="text-sm text-gray-500">시간마다</span>
                                    </div>
                                )}

                                {schedule.next_run && schedule.enabled && (
                                    <p className="text-sm text-purple-600 dark:text-purple-400">다음 실행: {new Date(schedule.next_run).toLocaleString('ko-KR')}</p>
                                )}
                                {schedule.deferred && (
                                    <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300" role="status">
                                        <Clock className="mt-0.5 h-4 w-4 shrink-0" />
                                        <span>예약 시각에 다른 작업이 진행 중이어서 대기하고 있습니다. 현재 작업이 끝나면 예약 수집을 한 번만 자동으로 시작합니다.</span>
                                    </div>
                                )}

                                <div className="text-xs leading-5 text-gray-500 dark:text-gray-400">
                                    예약 실행에도 요청 간격 조절·중복 제거·증분 수집을 적용합니다. 유료 API는 사용량에 따른 요금이 발생할 수 있습니다.
                                </div>

                                <button
                                    onClick={handleSaveSchedule}
                                    disabled={scheduleSaving}
                                    className="px-5 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 flex items-center gap-2"
                                >
                                    {scheduleSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                    예약 저장
                                </button>

                                {/* 앱 내 예약(APScheduler)은 앱이 켜져 있을 때만 동작 — OS 작업으로 보완 */}
                                <div className="border-t dark:border-gray-700 pt-4 mt-2">
                                    <div className="flex items-start justify-between gap-4">
                                        <div>
                                            <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-200">앱 종료 중에도 자동 실행</h3>
                                            {osTask.registered===true && <span className="inline-block mt-2 text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">등록됨</span>}
                                            {osTask.error && <p role="status" className="mt-2 text-xs text-gray-500">{osTask.error} <button type="button" disabled={osTaskSaving||osTaskReading} onClick={loadOsTask} className="underline underline-offset-2 disabled:opacity-50">등록 상태 다시 확인</button></p>}
                                        </div>
                                        {osTask.supported!==false ? (
                                            <Tooltip content={osTask.registered === true ? 'Windows에 등록한 자동 실행을 해제합니다.' : '설정한 예약을 Windows에 등록합니다.'}>
                                            <button
                                                onClick={handleToggleOsTask}
                                                disabled={osTaskSaving||osTaskReading||typeof osTask.registered!=='boolean'||Boolean(osTask.error)}
                                                className={`px-4 py-2 rounded-lg disabled:opacity-50 flex items-center gap-2 whitespace-nowrap text-sm ${osTask.registered ? 'border border-red-300 text-red-600 hover:bg-red-50 dark:hover:bg-red-950/20' : 'bg-gray-800 text-white hover:bg-gray-900 dark:bg-gray-700'}`}
                                            >
                                                {osTaskSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Clock className="w-4 h-4" />}
                                                {typeof osTask.registered!=='boolean'?'등록 상태 확인 필요':osTask.registered ? '자동 실행 해제' : '자동 실행 등록'}
                                            </button>
                                            </Tooltip>
                                        ) : (
                                            <span className="text-xs text-gray-400 whitespace-nowrap">Windows 전용</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* Notification Webhooks */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
                            <div className="p-6 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-[#22252b]">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <Bell className="w-5 h-5 text-amber-500" /> 알림 연결
                                </h2>
                                <p className="text-sm text-gray-500 mt-1">새 항목을 수집하거나 마감이 가까워지면 연결한 대화방으로 알려드립니다.</p>
                            </div>
                            <div className="p-6 space-y-5 max-w-2xl">
                                <div className="space-y-2">
                                    <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
                                        <MessageSquare className="w-4 h-4 text-indigo-500" /> Discord 알림 연결 주소 <span className="text-xs font-normal text-gray-400">(Webhook URL)</span>
                                        {discordConfigured && <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">설정됨</span>}
                                    </label>
                                    <input
                                        type="text"
                                        value={discordUrl}
                                        onChange={(e) => setDiscordUrl(e.target.value)}
                                        placeholder={discordConfigured ? '변경하려면 새 URL 입력 (비워두면 유지)' : 'https://discord.com/api/webhooks/...'}
                                        className="w-full px-4 py-2 border border-gray-200 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-gray-100"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
                                        <MessageSquare className="w-4 h-4 text-emerald-500" /> Google Chat 알림 연결 주소 <span className="text-xs font-normal text-gray-400">(Webhook URL)</span>
                                        {gchatConfigured && <span className="text-xs bg-green-100 text-green-700 px-2 py-0.5 rounded-full">설정됨</span>}
                                    </label>
                                    <input
                                        type="text"
                                        value={gchatUrl}
                                        onChange={(e) => setGchatUrl(e.target.value)}
                                        placeholder={gchatConfigured ? '변경하려면 새 URL 입력 (비워두면 유지)' : 'https://chat.googleapis.com/v1/spaces/.../messages?key=...'}
                                        className="w-full px-4 py-2 border border-gray-200 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-gray-100"
                                    />
                                    <p className="text-xs text-gray-400">Google Chat 스페이스 → 앱/통합 관리 → 웹훅에서 발급한 URL을 붙여넣으세요.</p>
                                </div>
                                <button
                                    onClick={handleSaveWebhooks}
                                    disabled={webhookSaving}
                                    className="px-5 py-2 bg-amber-500 text-white rounded-lg hover:bg-amber-600 disabled:opacity-50 flex items-center gap-2"
                                >
                                    {webhookSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                    알림 연결 저장
                                </button>
                            </div>
                        </div>

                    </motion.div>
                )}

                {(activeTab === 'connections' || activeTab === 'automation') && (
                    <motion.div
                        key={`connections-automation-${activeTab}`}
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                        className="space-y-6"
                    >
                        {activeTab === 'connections' && <>
                        {/* Notion 연동 */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-emerald-100 dark:border-emerald-900/30 overflow-hidden">
                            <div className="p-6 border-b border-emerald-100 dark:border-emerald-900/30 bg-emerald-50/50 dark:bg-emerald-950/10 flex justify-between items-center">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <UploadCloud className="w-5 h-5 text-emerald-500" /> Notion 연결
                                </h2>
                                <span className={`text-xs px-2 py-1 rounded-full flex items-center gap-1 ${notionTokenConfigured
                                    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                                    : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'}`}>
                                    {notionTokenConfigured ? <CheckCircle className="w-3.5 h-3.5" /> : <AlertTriangle className="w-3.5 h-3.5" />}
                                    {notionTokenConfigured ? '연결 토큰 저장됨' : '연결 필요'}
                                </span>
                            </div>
                            <div className="p-6 space-y-4">
                                <div className="rounded-xl border border-emerald-100 bg-emerald-50/50 px-4 py-3 dark:border-emerald-900/40 dark:bg-emerald-950/15">
                                    <p className="text-sm text-emerald-900 dark:text-emerald-200"><b>연결 토큰 하나</b>를 새 소식 등록과 정책 최신화가 함께 사용합니다.</p>

                                </div>
                                <div role="group" aria-label="Notion 연결 관리" className="grid grid-cols-1 gap-3 md:grid-cols-3">
                                    <Link to="/settings?tab=guide&step=policy&edit=notion" className="flex min-h-14 flex-col items-center justify-center rounded-xl px-3 py-2 text-center text-sm font-medium leading-5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 border border-gray-200 bg-white text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700"><span>Notion</span><span className="whitespace-nowrap">연결 확인·변경</span></Link>
                                    <Link to="/settings?tab=guide&step=news&edit=news" className="flex min-h-14 flex-col items-center justify-center rounded-xl px-3 py-2 text-center text-sm font-medium leading-5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 border border-gray-200 bg-white text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700"><span>Notion</span><span className="whitespace-nowrap">새 소식 연결</span></Link>
                                <Link to="/settings?tab=guide&step=policy&edit=policy" className="flex min-h-14 flex-col items-center justify-center rounded-xl px-3 py-2 text-center text-sm font-medium leading-5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-600 border border-gray-200 bg-white text-gray-800 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:hover:bg-gray-700"><span>Notion</span><span className="whitespace-nowrap">정책 최신화 연결</span></Link>
                                </div>
                                {disconnectedPolicyData.count > 0 && (
                                    <div className="rounded-xl border border-amber-200 dark:border-amber-900/50 bg-amber-50/70 dark:bg-amber-950/20 p-4 space-y-3">
                                        <div>
                                            <p className="text-sm font-semibold text-amber-900 dark:text-amber-200">연결 해제된 로컬 자료가 남아 있습니다</p>
                                            <p className="mt-1 text-xs text-amber-800 dark:text-amber-300 leading-relaxed">
                                                {disconnectedPolicyData.count}개 보관함 · 정책 {disconnectedPolicyData.policy_count}건입니다. 앱의 로컬 사본만 차지하며 Notion 원본에는 영향이 없습니다. 다시 연결할 계획이면 그대로 두어도 됩니다.
                                            </p>
                                        </div>
                                        <button type="button" onClick={handleCleanupDisconnectedPolicyData} disabled={disconnectedCleanupLoading}
                                            className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-amber-300 dark:border-amber-800 text-sm font-medium text-amber-800 dark:text-amber-200 hover:bg-amber-100 dark:hover:bg-amber-950/40 disabled:opacity-50">
                                            {disconnectedCleanupLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                                            백업 후 로컬 자료 정리
                                        </button>
                                    </div>
                                )}
                            </div>
                        </div>


                        </>}

                        {/* (U2) 정책 최신화 무료 자동 감시 */}
                        {activeTab === 'automation' && (
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-indigo-100 dark:border-indigo-900/30 overflow-hidden">
                            <div className="p-6 border-b border-indigo-100 dark:border-indigo-900/30 bg-indigo-50/50 dark:bg-indigo-950/10 flex justify-between items-center">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <Clock className="w-5 h-5 text-indigo-500" /> 정책 최신화 자동 감시
                                </h2>
                                <label className="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" className="sr-only peer"
                                        checked={policyWatchSchedule.enabled}
                                        onChange={(e) => handleSavePolicyWatchSchedule({ ...policyWatchSchedule, enabled: e.target.checked })} />
                                    <div className="w-11 h-6 bg-gray-200 peer-focus:outline-none rounded-full peer dark:bg-gray-700 peer-checked:bg-indigo-600 transition-colors" />
                                    <div className="absolute left-1 top-1 w-4 h-4 bg-white rounded-full transition-transform peer-checked:translate-x-5" />
                                </label>
                            </div>
                            <div className="p-6 space-y-4">
                                <p className="text-sm text-gray-500">
                                    출처 변경 시 알림을 보냅니다. 내용은 자동으로 수정되지 않습니다. 정책 최신화 화면에서 변경점을 확인하고 반영해주세요.
                                </p>
                                <div className="flex gap-3">
                                    <div>
                                        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">요일</label>
                                        <select value={policyWatchSchedule.day_of_week}
                                            onChange={(e) => setPolicyWatchSchedule(prev => ({ ...prev, day_of_week: e.target.value }))}
                                            className="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm">
                                            {[['mon', '월'], ['tue', '화'], ['wed', '수'], ['thu', '목'], ['fri', '금'], ['sat', '토'], ['sun', '일']].map(([v, l]) => (
                                                <option key={v} value={v}>{l}요일</option>
                                            ))}
                                        </select>
                                    </div>
                                    <div>
                                        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">시각</label>
                                        <input type="time" value={policyWatchSchedule.time}
                                            onChange={(e) => setPolicyWatchSchedule(prev => ({ ...prev, time: e.target.value }))}
                                            className="w-full mt-1 px-3 py-2 border rounded-lg dark:bg-gray-800 dark:border-gray-600 dark:text-gray-100 text-sm" />
                                    </div>
                                </div>
                                {policyWatchSchedule.next_run && (
                                    <p className="text-xs text-gray-400">다음 실행: {new Date(policyWatchSchedule.next_run).toLocaleString('ko-KR')}</p>
                                )}
                                <button onClick={() => handleSavePolicyWatchSchedule(policyWatchSchedule)} disabled={policyWatchSaving}
                                    className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 flex items-center gap-2 text-sm">
                                    {policyWatchSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                    저장
                                </button>
                            </div>
                        </div>
                        )}
                    </motion.div>
                )}

                {activeTab === 'system' && (
                    <motion.div
                        key="system"
                        id="settings-panel-system"
                        role="tabpanel"
                        aria-labelledby="settings-tab-system"
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 10 }}
                        className="space-y-6"
                    >
                        {/* Health Monitor — 표가 길어 실제로 확인할 때만 연다. */}
                        <details open={systemHealthOpen} onToggle={(event) => setSystemHealthOpen(event.currentTarget.open)} className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-[#1e2128]">
                            <summary className="cursor-pointer list-none rounded-xl px-5 py-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500">
                                <span className="flex items-center justify-between gap-4">
                                    <span>
                                        <span className="flex items-center gap-2 text-sm font-semibold text-gray-800 dark:text-gray-100"><Activity className="h-4 w-4 text-blue-500" /> 사이트 수집 상태</span>
                                        <span className="mt-1 block text-xs font-normal text-gray-500">최근 7일 동안 사이트별로 새 자료가 들어왔는지 확인합니다.</span>
                                    </span>
                                    <span className="shrink-0 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-600 dark:bg-blue-950/30 dark:text-blue-300">상태 확인</span>
                                </span>
                            </summary>
                            <div className="border-t border-gray-100 dark:border-gray-700">
                            <div className="p-6 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-[#22252b] flex justify-between items-center">
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                    <Activity className="w-5 h-5 text-blue-500" /> 사이트별 상세 상태
                                </h3>
                                <button onClick={loadHealth} className="text-sm text-blue-500 hover:underline">새로고침</button>
                            </div>
                            <div className="p-6">
                                <p className="text-xs text-gray-400 mb-3">
                                    사이트별 <b>최근 7일간 신규 수집 여부</b>만 봅니다 — 7일 넘게 새 항목이 없으면 "점검 필요"로 표시됩니다.
                                    (실행 성공/실패 이력까지 자세히 보려면 "사이트 관리" 페이지의 사이트 건강 대시보드를 확인하세요.)
                                </p>
                                {healthLoading ? <div className="text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 로딩 중...</div> : (
                                    <div className="overflow-x-auto">
                                        <table className="w-full text-sm text-left">
                                            <thead className="text-xs text-gray-500 uppercase bg-gray-50 dark:bg-gray-800 dark:text-gray-400">
                                                <tr>
                                                    <th className="px-4 py-3">사이트명</th>
                                                    <th className="px-4 py-3">총 수집</th>
                                                    <th className="px-4 py-3">마지막 활동</th>
                                                    <th className="px-4 py-3">상태</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {healthStats.map((site, i) => (
                                                    <tr key={i} className="border-b dark:border-gray-700">
                                                        <td className="px-4 py-3 font-medium">{site.site_name}</td>
                                                        <td className="px-4 py-3">{site.total_items}건</td>
                                                        <td className="px-4 py-3">{site.last_activity ? new Date(site.last_activity).toLocaleDateString() : '-'}</td>
                                                        <td className="px-4 py-3">
                                                            {site.status === 'good' ? (
                                                                <span className="flex items-center gap-1 text-green-600"><CheckCircle className="w-4 h-4" /> 정상</span>
                                                            ) : site.status === 'warning' ? (
                                                                <span className="flex items-center gap-1 text-orange-500"><AlertTriangle className="w-4 h-4" /> 점검 필요</span>
                                                            ) : (
                                                                <span className="text-gray-400">대기</span>
                                                            )}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </div>
                        </div>
                        </details>

                        {/* 가장 중요한 안전 기능은 고급 영역 밖에 둔다. */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-indigo-100 dark:border-indigo-900/30 overflow-hidden">
                            <div className="p-6 border-b border-indigo-100 dark:border-indigo-900/30 bg-indigo-50/50 dark:bg-indigo-950/10">
                                <h2 className="ui-section-title flex items-center gap-2">
                                    <Database className="w-5 h-5 text-indigo-500" /> 데이터 백업 및 복원
                                </h2>
                            </div>
                            <div className="p-6 space-y-6">
                                <div className="flex flex-wrap items-center justify-between gap-4">
                                    <div className="min-w-0 flex-1 basis-64">
                                        <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-200">백업 다운로드</h3>
                                        <p className="text-sm text-gray-600 dark:text-gray-300 mt-1">저장된 수집 자료와 정책을 복원용 파일(.db)로 내려받습니다.</p>
                                    </div>
                                    <button type="button" onClick={handleBackup} className="min-h-11 shrink-0 px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 flex items-center gap-2 transition-colors">
                                        <Download className="w-4 h-4" /> 백업 받기
                                    </button>
                                </div>
                                <div className="border-t dark:border-gray-700 pt-6 flex flex-wrap items-center justify-between gap-4">
                                    <div className="min-w-0 flex-1 basis-64">
                                        <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-200">데이터 복원</h3>
                                        <p className="text-sm text-gray-600 dark:text-gray-300 mt-1">이전에 백업한 .db 파일로 복구합니다. <span className="text-red-700 dark:text-red-300">현재 데이터는 안전 백업 후 교체됩니다.</span></p>
                                    </div>
                                    <label className="relative inline-flex min-h-11 shrink-0 cursor-pointer rounded-lg focus-within:ring-2 focus-within:ring-blue-600 focus-within:ring-offset-2">
                                        <input type="file" accept=".db" onChange={handleRestore} aria-label="복원할 백업 파일 선택" className="absolute inset-0 h-full w-full opacity-0 cursor-pointer" title="백업 파일 선택" />
                                        <span aria-hidden="true" className="px-4 py-2 border border-gray-200 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-2 transition-colors pointer-events-none">
                                            <Upload className="w-4 h-4" /> 파일 업로드
                                        </span>
                                    </label>
                                </div>
                            </div>
                        </div>

                        <details open={systemAdvancedOpen} onToggle={(event) => setSystemAdvancedOpen(event.currentTarget.open)} className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-[#1e2128]">
                            <summary className="cursor-pointer list-none rounded-xl px-5 py-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500">
                                <span className="flex items-center justify-between gap-4">
                                    <span>
                                        <span className="block text-sm font-semibold text-gray-800 dark:text-gray-100">고급 시스템 관리</span>
                                        <span className="mt-1 block text-xs font-normal text-gray-500">문제 진단·만료 자료 정리·검색 데이터 재생성처럼 가끔 쓰는 기능입니다.</span>
                                    </span>
                                    <span className="shrink-0 rounded-full bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-500 dark:bg-gray-800 dark:text-gray-300">필요할 때만</span>
                                </span>
                            </summary>
                            <div className="space-y-5 border-t border-gray-100 p-4 dark:border-gray-700 sm:p-5">

                        {/* 실시간 로그 — (2026-07-20) 대시보드에 있던 SYSTEM LOGS 박스를 이곳으로 이동.
                            일반 사용자가 매번 보는 화면이 아니라, 문제 진단이 필요할 때만 찾는
                            "시스템 관리" 성격의 정보라 대시보드보다 이 탭이 맞는 자리다. */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
                            <div className="p-6 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-[#22252b]">
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                    <Activity className="w-5 h-5 text-gray-500" /> 실시간 로그
                                </h3>
                                <p className="text-xs text-gray-400 mt-1">문제가 발생했을 때 원인 파악용입니다. 평소엔 확인하지 않아도 됩니다.</p>
                            </div>
                            <div className="p-4">
                                <div className="h-28 overflow-y-auto font-mono text-[11px] space-y-0.5 bg-[#1a1d23] text-gray-300 rounded-lg p-3 scrollbar-thin scrollbar-thumb-gray-700 scrollbar-track-transparent">
                                    {liveLogs && liveLogs.length > 0 ? (
                                        liveLogs.slice().reverse().map((log, i) => (
                                            <div key={i} className="hover:text-white transition-colors truncate">{log}</div>
                                        ))
                                    ) : (
                                        <div className="text-gray-600 italic">대기 중...</div>
                                    )}
                                </div>
                            </div>
                        </div>

                        {/* Archive Expired */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-yellow-100 dark:border-yellow-900/30 overflow-hidden">
                            <div className="p-6 border-b border-yellow-100 dark:border-yellow-900/30 bg-yellow-50/50 dark:bg-yellow-950/10">
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                    <AlertTriangle className="w-5 h-5 text-yellow-500" /> 만료 항목 아카이브
                                </h3>
                            </div>
                            <div className="p-6 flex items-center justify-between">
                                <div>
                                    <p className="font-medium text-gray-800 dark:text-gray-200">마감 경과 항목 숨기기</p>
                                    <p className="text-sm text-gray-500 mt-1">마감일이 지난 항목을 아카이브 처리합니다. 데이터는 보존되며 검색에서만 숨겨집니다.</p>
                                </div>
                                <button
                                    onClick={handleArchiveExpired}
                                    disabled={archiveLoading}
                                    className="px-4 py-2 bg-yellow-500 text-white rounded-lg hover:bg-yellow-600 disabled:opacity-50 flex items-center gap-2"
                                >
                                    {archiveLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <AlertTriangle className="w-4 h-4" />}
                                    아카이브 실행
                                </button>
                            </div>
                        </div>

                        {/* Bulk Cleaner */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-orange-100 dark:border-orange-900/30 overflow-hidden">
                            <div className="p-6 border-b border-orange-100 dark:border-orange-900/30 bg-orange-50/50 dark:bg-orange-950/10">
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                    <Trash2 className="w-5 h-5 text-orange-500" /> 데이터 정리
                                </h3>
                            </div>
                            <div className="p-6 space-y-5">
                                <div className="flex items-center justify-between">
                                    <div>
                                        <p className="font-medium text-gray-800 dark:text-gray-200">오래된 데이터 삭제</p>
                                        <p className="text-sm text-gray-500 mt-1">90일 이상 지난 데이터와 이미 마감된지 30일 지난 공고를 삭제하여 DB를 최적화합니다.</p>
                                    </div>
                                    <button
                                        onClick={handleCleanup}
                                        disabled={cleanupLoading}
                                        className="px-4 py-2 bg-orange-600 text-white rounded-lg hover:bg-orange-700 disabled:opacity-50 flex items-center gap-2 whitespace-nowrap"
                                    >
                                        {cleanupLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                                        정리 시작
                                    </button>
                                </div>

                                <NewsRecordRetentionSettings />

                                {/* 자동 보존기간 (무한 누적 방지) */}
                                <div className="border-t dark:border-gray-700 pt-5">
                                    <div className="flex items-start justify-between gap-4">
                                        <div className="flex-1">
                                            <p className="font-medium text-gray-800 dark:text-gray-200">자동 보존기간</p>
                                            <p className="text-xs text-gray-500 mt-1">
                                                수집 후 하루 1회, 설정한 보존기간보다 오래된 자료 또는 마감 후 30일이 지난 공고를 자동 삭제합니다. 0으로 두면 자동 삭제를 끕니다.
                                            </p>
                                            <div className="flex items-center gap-2 mt-3">
                                                <input
                                                    type="number"
                                                    min={0}
                                                    max={3650}
                                                    value={retentionDays}
                                                    onChange={(e) => setRetentionDays(e.target.value)}
                                                    className="w-28 px-3 py-2 border border-gray-200 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-gray-100"
                                                />
                                                <span className="text-sm text-gray-500">일 보관</span>
                                            </div>
                                        </div>
                                        <button
                                            onClick={handleSaveRetention}
                                            disabled={retentionSaving}
                                            className="px-4 py-2 bg-gray-800 dark:bg-gray-700 text-white rounded-lg hover:bg-gray-900 disabled:opacity-50 flex items-center gap-2 whitespace-nowrap"
                                        >
                                            {retentionSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                                            보존기간 저장
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>

                        {/* 고급 도구 — 자주 안 쓰는 1회성 기능 모음 (접이식) */}
                        <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
                            <button
                                onClick={() => setAdvancedOpen(o => !o)}
                                className="w-full p-6 flex justify-between items-center hover:bg-gray-50/50 dark:hover:bg-[#22252b] transition-colors"
                            >
                                <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                    고급 도구 <span className="text-xs font-normal text-gray-400">(자주 쓰지 않는 1회성 기능)</span>
                                </h3>
                                {advancedOpen ? <ChevronUp className="w-5 h-5 text-gray-400" /> : <ChevronDown className="w-5 h-5 text-gray-400" />}
                            </button>
                            {advancedOpen && (
                                <div className="p-6 pt-0 space-y-6 border-t border-gray-100 dark:border-gray-700">
                                    {/* Embedding (Semantic Search) Backfill */}
                                    <div className="border border-purple-100 dark:border-purple-900/30 rounded-xl overflow-hidden mt-6">
                                        <div className="p-4 border-b border-purple-100 dark:border-purple-900/30 bg-purple-50/50 dark:bg-purple-950/10 flex justify-between items-center">
                                            <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                                                <Cpu className="w-4 h-4 text-purple-500" /> 의미 검색 데이터
                                            </h3>
                                            <button onClick={loadEmbStatus} className="text-xs text-purple-500 hover:underline flex items-center gap-1">
                                                <RefreshCw className="w-3 h-3" /> 새로고침
                                            </button>
                                        </div>
                                        <div className="p-4 flex items-center justify-between gap-4">
                                            <div className="flex-1">
                                                <p className="text-sm text-gray-700 dark:text-gray-300">기존 항목의 의미 검색 데이터 만들기</p>
                                                <p className="text-xs text-gray-500 mt-1">의미 검색(AI)을 쓰려면 기존 항목의 의미 검색 데이터를 한 번 만들어야 합니다. 이후 새로 수집되는 항목은 자동으로 만들어집니다.</p>
                                                {embStatus && (
                                                    <div className="mt-3">
                                                        <div className="flex justify-between text-xs text-gray-500 mb-1">
                                                            <span>인덱스 생성: {embStatus.embedded} / {embStatus.total}</span>
                                                            <span>{embStatus.total > 0 ? Math.round((embStatus.embedded / embStatus.total) * 100) : 0}%</span>
                                                        </div>
                                                        <div className="w-full h-2 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                                                            <div className="h-full bg-purple-500 transition-all" style={{ width: `${embStatus.total > 0 ? (embStatus.embedded / embStatus.total) * 100 : 0}%` }} />
                                                        </div>
                                                    </div>
                                                )}
                                            </div>
                                            {backfillLoading ? (
                                                <button
                                                    onClick={handleCancelBackfill}
                                                    className="px-3 py-1.5 text-sm border border-red-300 text-red-600 rounded-lg hover:bg-red-50 dark:hover:bg-red-950/20 flex items-center gap-2 whitespace-nowrap"
                                                >
                                                    <Loader2 className="w-4 h-4 animate-spin" /> 생성 중… (취소)
                                                </button>
                                            ) : (
                                                <button
                                                    onClick={handleBackfill}
                                                    disabled={(embStatus && embStatus.missing === 0) || aiStatus?.busy}
                                                    className="px-3 py-1.5 text-sm bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 flex items-center gap-2 whitespace-nowrap"
                                                >
                                                    <Sparkles className="w-4 h-4" />
                                                    {embStatus && embStatus.missing === 0 ? '완료됨' : '의미 검색 데이터 만들기'}
                                                </button>
                                            )}
                                        </div>
                                    </div>

                                </div>
                            )}
                        </div>
                                <LogsTab />
                            </div>
                        </details>
                    </motion.div>
                )}
            </AnimatePresence>
        </div>
    );
};

// Sub-component for Logs Tab to keep main component clean
const LogsTab = () => {
    const [logs, setLogs] = useState([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        loadLogs();
    }, []);

    const loadLogs = async () => {
        setLoading(true);
        try {
            const files = await welfareApi.getLogFiles();
            setLogs(files);
        } catch (error) {
            console.error("Failed to load logs:", error);
            toast.error('로그 목록을 불러오지 못했습니다.');
        } finally {
            setLoading(false);
        }
    };

    const handleDownload = async (filename) => {
        try {
            await welfareApi.downloadLog(filename);
            toast.success(`${filename} 다운로드 시작`);
        } catch (error) {
            toast.error('다운로드 실패');
        }
    };

    const handleDelete = async (filename) => {
        if (!window.confirm(`정말 ${filename} 파일을 삭제하시겠습니까?`)) return;
        try {
            await welfareApi.deleteLog(filename);
            toast.success('삭제되었습니다');
            loadLogs();
        } catch (error) {
            toast.error('삭제 실패');
        }
    };

    return (
        <motion.div
            key="logs"
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 10 }}
            className="space-y-6"
        >
            <div className="bg-white dark:bg-[#1e2128] rounded-xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
                <div className="p-6 border-b border-gray-100 dark:border-gray-700 bg-gray-50/50 dark:bg-[#22252b] flex justify-between items-center">
                    <h3 className="text-base font-semibold leading-snug text-gray-800 dark:text-gray-100 flex items-center gap-2">
                        <FileText className="w-5 h-5 text-gray-500" /> 시스템 로그 파일
                    </h3>
                    <button onClick={loadLogs} className="text-sm text-blue-500 hover:underline">새로고침</button>
                </div>
                <div className="p-6">
                    {loading ? <div className="text-gray-400 flex gap-2"><Loader2 className="animate-spin" /> 로딩 중...</div> : logs.length === 0 ? (
                        <p className="text-gray-500 text-center py-4">로그 파일이 없습니다.</p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="w-full text-sm text-left">
                                <thead className="text-xs text-gray-500 uppercase bg-gray-50 dark:bg-gray-800 dark:text-gray-400">
                                    <tr>
                                        <th className="px-4 py-3">파일명</th>
                                        <th className="px-4 py-3">크기</th>
                                        <th className="px-4 py-3">수정일</th>
                                        <th className="px-4 py-3 text-right">관리</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {logs.map((log, i) => (
                                        <tr key={i} className="border-b dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors">
                                            <td className="px-4 py-3 font-medium flex items-center gap-2">
                                                <FileText className="w-4 h-4 text-gray-400" />
                                                {log.name}
                                            </td>
                                            <td className="px-4 py-3 text-gray-500">{(log.size / 1024).toFixed(1)} KB</td>
                                            <td className="px-4 py-3 text-gray-500">{new Date(log.modified * 1000).toLocaleString()}</td>
                                            <td className="px-4 py-3 text-right flex justify-end gap-2">
                                                <button
                                                    onClick={() => handleDownload(log.name)}
                                                    className="p-1.5 text-blue-600 hover:bg-blue-50 rounded-lg transition-colors border border-transparent hover:border-blue-200"
                                                    title="다운로드"
                                                >
                                                    <Download className="w-4 h-4" />
                                                </button>
                                                <button
                                                    onClick={() => handleDelete(log.name)}
                                                    className="p-1.5 text-red-600 hover:bg-red-50 rounded-lg transition-colors border border-transparent hover:border-red-200"
                                                    title="삭제"
                                                >
                                                    <Trash2 className="w-4 h-4" />
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            </div>
        </motion.div>
    );
};

const SettingsIcon = ({ className }) => (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}>
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.1a2 2 0 0 1-1-1.74v-.47a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
        <circle cx="12" cy="12" r="3" />
    </svg>
);

export default Settings;
