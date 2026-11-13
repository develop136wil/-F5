import React, { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, ArrowRight, Check, CheckCircle2, Globe, Info, KeyRound, Loader2, MapPin, Plus, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { welfareApi } from '../../api/welfare';
import { queryKeys } from '../../api/queryKeys';
import { applyVerifiedPolicyTitles, createPolicyDbDraft, parsePolicyDbs, policyDbPayload } from '../../constants/policyDbs';
import { NOTION_NEWS_TEMPLATE_URL, NOTION_TEMPLATE_URL } from '../../constants/onboardingLinks';
import { getErrorMessage } from '../../utils/error';
import { currentVerification } from '../../utils/verification';
import { buildOnboardingProgress } from '../../utils/tutorialStatus';
import SetupWalkthrough, { SetupStageInstructions } from './SetupWalkthrough';
import SetupSteps from './SetupSteps';
import SetupTemplateChooser from './SetupTemplateChooser';
import DefaultNewsConditionEditor from '../DefaultNewsConditionEditor';
import { useSettingsDraft } from '../../hooks/useSettingsDraft';
import { settingsDrafts } from '../../utils/settingsDrafts';
import { firstIncompleteSetupStep, nextSetupStep, featureSetupSteps, featureSetupItems, isConnectionFormVisible } from '../../utils/setupNavigation';

const saveDraftConfig = payload => settingsDrafts.runWrite(() => welfareApi.saveConfig(payload));

const STEP_STORAGE_KEY = 'welfare-f5-onboarding-step';
const CORE_STEPS = [
    { key: 'google', label: 'AI 연결', shortLabel: 'AI', icon: KeyRound },
    { key: 'target', label: '수집 대상', shortLabel: '대상', icon: MapPin },
    { key: 'sites', label: '사이트', icon: Globe },
];

const FEATURE_GUIDES = {
    policy: {
        title: '정책 최신화 준비',
        description: 'Gemini 설정부터 Notion 가입, 템플릿 복제와 주소 등록까지 한 흐름으로 준비합니다.',
    },
    news: {
        title: '새 소식 Notion 저장 준비',
        description: 'Gemini 설정부터 내 Notion 보관함 주소 등록까지 안내합니다. AI 없이 저장하거나 Notion 없이 수집할 수도 있습니다.',
    },
};

const inputClass = 'w-full px-3 py-2.5 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-500';

const ObjectiveSteps = ({ items, completion }) => (
    <div className="py-2">
        <p className="text-sm font-medium text-gray-800 dark:text-gray-100">이번 단계에서 할 일</p>
        <ol className="mt-2 space-y-1.5">
            {items.map((item, index) => <li key={item} className="flex gap-2 text-sm leading-6 text-gray-600 dark:text-gray-300"><span className="font-medium text-blue-600">{index + 1}</span><span>{item}</span></li>)}
        </ol>
        <p className="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">완료 조건: {completion}</p>
    </div>
);

const CompletedConnection = ({ title, description, onEdit, editLabel }) => (
    <div className="w-full py-2">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
                <p className="flex items-center gap-2 text-sm font-medium text-emerald-800 dark:text-emerald-200"><CheckCircle2 className="h-4 w-4" /> {title}</p>
                <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">{description}</p>
            </div>
            {onEdit ? <button type="button" onClick={onEdit} className="min-h-10 shrink-0 rounded-lg border border-emerald-300 px-4 py-2 text-sm font-semibold text-emerald-700 dark:text-emerald-300">{editLabel}</button> : null}
        </div>
    </div>
);

const SetupSection = ({ children, open, id }) => {
    if (!open) return null;
    const [heading, ...body] = React.Children.toArray(children);
    return <section className="w-full space-y-6">
        <div className="flex items-center gap-3">
            <div className="min-w-0 flex-1">{heading}</div>
        </div>
        <div id={id} hidden={!open} className="space-y-6">{body}</div>
    </section>;
};

const GuidedSetup = () => {
    const queryClient = useQueryClient();
    const guideRef = useRef(null);
    const location = useLocation();
    const navigate = useNavigate();
    const requestedGuideStep = new URLSearchParams(location.search).get('step');
    const requestedEdit = new URLSearchParams(location.search).get('edit');
    const [editing, setEditing] = useState(requestedEdit);
    useEffect(() => { setEditing(requestedEdit); }, [requestedEdit]);
    const unified = requestedGuideStep === 'setup';
    const [templateChoice, setTemplateChoice] = useState(null);
    const featureMode = unified ? (templateChoice || 'policy') : Object.hasOwn(FEATURE_GUIDES, requestedGuideStep) ? requestedGuideStep : null;
    const [selectedSection, setSelectedSection] = useState(null);
    const [acknowledged, setAcknowledged] = useState({});
    const [skipNewsAi, setSkipNewsAi] = useState(false);
    const [stepIndex, setStepIndex] = useState(() => {
        if (requestedGuideStep === 'complete') return CORE_STEPS.length;
        const requestedIndex = CORE_STEPS.findIndex(step => step.key === requestedGuideStep);
        if (requestedIndex >= 0) return requestedIndex;
        const saved = Number(window.localStorage.getItem(STEP_STORAGE_KEY));
        return Number.isInteger(saved) && saved >= 0 && saved <= CORE_STEPS.length ? saved : CORE_STEPS.length;
    });
    // Compare visits, not just URLs: leaving and returning must invalidate old UI responses.
    const viewRef = useRef({ identity: null, generation: 0, mounted: true });
    const viewIdentity = `${location.key}:${location.pathname}:${location.search}:${stepIndex}:${selectedSection?.mode}:${selectedSection?.key}`;
    if (viewRef.current.identity !== viewIdentity) {
        viewRef.current.identity = viewIdentity;
        viewRef.current.generation++;
    }
    const viewGeneration = viewRef.current.generation;
    const isCurrentView = () => viewRef.current.mounted && viewRef.current.generation === viewGeneration;
    useEffect(() => {
        viewRef.current.mounted = true;
        return () => { viewRef.current.mounted = false; };
    }, []);
    const saveToast = Object.fromEntries(['success', 'warning', 'error'].map(type => [type, message => {
        if (isCurrentView()) toast[type](message);
    }]));
    useEffect(() => {
        const index = CORE_STEPS.findIndex(step => step.key === requestedGuideStep);
        if (index >= 0) setStepIndex(index);
        else if (requestedGuideStep === 'complete') setStepIndex(CORE_STEPS.length);
    }, [requestedGuideStep]);
    const [saving, setSaving] = useState(false);
    const [statusMessage, setStatusMessage] = useState('');
    const [googleKey, setGoogleKey] = useSettingsDraft('apiKey', '');
    const [notionToken, setNotionToken] = useSettingsDraft('notionToken', '');
    const [newsDbId, setNewsDbId] = useSettingsDraft('notionDbId', '');
    const [policyDbs, setPolicyDbs] = useSettingsDraft('policyDbs', () => [createPolicyDbDraft(0)]);

    const { data: config } = useQuery({ queryKey: queryKeys.config, queryFn: welfareApi.getConfig, refetchOnMount: 'always' });
    const defaultCondition = useQuery({queryKey:['defaultNewsCondition'],queryFn:welfareApi.getDefaultNewsCondition});
    const { data: sites } = useQuery({ queryKey: queryKeys.sites, queryFn: welfareApi.getSites });
    const defaultReadiness = useQuery({queryKey:['defaultNewsReadiness',defaultCondition.data?.profile?.revision,sites?.length],
        queryFn:()=>welfareApi.getNewsWorkflowOptions('default-news'),enabled:Boolean(defaultCondition.data?.profile)});
    const { data: savedGoogleCheck, isFetching: checkingGoogle } = useQuery({
        queryKey: ['tutorialGoogleVerification', config?.revision], queryFn: welfareApi.verifyGoogleApiKey,
        enabled: Boolean(config?.is_configured), staleTime: 5 * 60 * 1000, retry: false,
    });
    const { data: cachedPolicyCheck, isFetching: checkingPolicy } = useQuery({
        queryKey: ['tutorialPolicyDbVerification', config?.revision], queryFn: welfareApi.verifyPolicyDbs,
        enabled: Boolean(config?.notion_token_configured && config?.NOTION_POLICY_DBS), staleTime: 5 * 60 * 1000, retry: false,
    });
    const { data: savedNotionCheck, isFetching: checkingNotion } = useQuery({
        queryKey: ['tutorialNotionVerification', config?.revision], queryFn: welfareApi.verifyNotion,
        enabled: Boolean(config?.notion_configured), staleTime: 5 * 60 * 1000, retry: false,
    });

    useEffect(() => {
        setStatusMessage('');
        const mainScrollArea = guideRef.current?.closest('main');
        if (mainScrollArea) mainScrollArea.scrollTo({ top: 0, behavior: 'auto' });
        else guideRef.current?.scrollIntoView({ block: 'start', behavior: 'auto' });
        if (!featureMode) window.localStorage.setItem(STEP_STORAGE_KEY, String(stepIndex));
        window.localStorage.setItem('welfare-f5-setup-resume', unified ? 'setup' : featureMode || CORE_STEPS[stepIndex]?.key || 'complete');
    }, [featureMode, stepIndex]);

    useEffect(() => {
        if (!config) return;
        // Read fresh data: the shared query cache may predate a save in Settings.
        let cancelled = false;
        const request = settingsDrafts.capture();
        welfareApi.getConfig().then(fresh => {
            if (cancelled) return;
            const saved = parsePolicyDbs(fresh.NOTION_POLICY_DBS);
            settingsDrafts.hydrate({notionDbId:fresh.NOTION_DATABASE_ID || '', policyDbs:saved.length ? saved : [createPolicyDbDraft(0)]}, request);
        }).catch(() => { if (!cancelled) setStatusMessage('저장된 설정을 다시 확인하지 못했습니다. 입력 중인 내용은 그대로 유지했습니다.'); });
        return () => { cancelled = true; };
    }, [config]);

    const currentCheck = (...checks) => currentVerification(config?.revision, ...checks);
    const effectiveGoogleCheck = currentCheck(savedGoogleCheck);
    const effectiveNotionCheck = currentCheck(savedNotionCheck);
    const effectivePolicyCheck = currentCheck(cachedPolicyCheck);
    const newsGalleryReady = Boolean(config?.notion_configured && effectiveNotionCheck?.ok) && !settingsDrafts.dirty('notionDbId') && !settingsDrafts.dirty('notionToken');
    const done = {
        google: Boolean(effectiveGoogleCheck?.ok) && !settingsDrafts.dirty('apiKey'),
        target: Boolean(defaultCondition.data?.profile) && !settingsDrafts.dirty('defaultNewsConditionDraft'),
        sites: (sites?.length || 0) > 0,
        // 연결 토큰만 저장해도 이 단계의 완료 조건은 충족한다. 새 소식 DB 또는 정책 DB
        // 검증까지 요구하면 화면의 "키 저장 완료" 안내와 체크 표시가 서로 어긋난다.
        notion: Boolean(config?.notion_token_configured) && !settingsDrafts.dirty('notionToken'),
        news: newsGalleryReady,
        policy: Boolean(config?.notion_token_configured && effectivePolicyCheck?.ok) && !settingsDrafts.dirty('policyDbs') && !settingsDrafts.dirty('notionToken'),
    };
    done.signup = done.notion || Boolean(acknowledged.signup);
    done.template = Boolean(done[featureMode] || acknowledged[`${featureMode}-template`]);
    if (editing && Object.hasOwn(done, editing)) done[editing] = false;
    const flowDone = {...done, google: done.google || (featureMode === 'news' && skipNewsAi)};
    const showNewsForm = isConnectionFormVisible(newsGalleryReady, editing, 'news');
    const baseProgress = buildOnboardingProgress(done);
    const progress = {...baseProgress, collectionReady:baseProgress.collectionReady && Boolean(defaultReadiness.data?.enabled)};
    const checkingConnections = !config || checkingGoogle || (featureMode === 'policy' ? checkingPolicy : featureMode === 'news' ? checkingNotion : false);
    const activeSection = selectedSection?.mode === featureMode ? selectedSection.key : null;
    useEffect(() => {
        if (!activeSection) return;
        const heading = document.getElementById(`setup-section-${activeSection}`)?.parentElement?.querySelector('h3');
        if (heading) { heading.tabIndex = -1; heading.focus({preventScroll:true}); }
        guideRef.current?.closest('dialog')?.scrollTo({top:0,behavior:'auto'});
    }, [activeSection]);
    useEffect(() => {
        if (!featureMode || checkingConnections || (selectedSection?.mode === featureMode && selectedSection.key)) return;
        setSelectedSection(previous => {
            const key = editing || (unified && !templateChoice ? ['google','signup','notion'].find(key=>!flowDone[key]) || 'template' : firstIncompleteSetupStep(featureMode, flowDone) || 'google');
            return previous?.mode === featureMode && previous.key === key ? previous : {mode: featureMode, key};
        });
    }, [featureMode, checkingConnections, selectedSection, editing, done.google, done.notion, done.news, done.policy, done.signup, done.template, skipNewsAi, unified, templateChoice]);
    const sectionProps = key => ({
        id: `setup-section-${key}`,
        open: activeSection === key,
        disabled: saving || checkingConnections,
        onToggle: () => setSelectedSection({ mode: featureMode, key: activeSection === key ? null : key }),
    });
    const advanceAfterSave = key => {
        // A save may finish after the user has navigated to a different guide.
        if (!isCurrentView()) return;
        const fields = { google: ['apiKey'], target: ['defaultNewsConditionDraft'], notion: ['notionToken'], news: ['notionDbId', 'notionToken'], policy: ['policyDbs', 'notionToken'] };
        if (fields[key]?.some(field => settingsDrafts.dirty(field))) {
            announce('먼저 요청한 내용은 저장했습니다. 그 뒤에 수정한 내용은 아직 저장되지 않았습니다. 확인 후 다시 저장해주세요.');
            return;
        }
        setEditing(null);
        if (featureMode && key === featureMode) {
            if (unified) {
                setTemplateChoice(null);
                setSelectedSection({mode:'policy',key:'template'});
                return;
            }
            navigate('/settings?tab=guide', {replace:true});
            return;
        }
        if (featureMode) setSelectedSection({ mode: featureMode, key: unified && !templateChoice && key === 'notion' ? 'template' : nextSetupStep(featureMode, key, flowDone) });
        else if (key === 'google' || key === 'target') setStepIndex(key === 'google' ? 1 : 2);
    };
    const current = CORE_STEPS[stepIndex];
    const isCompleteScreen = !featureMode && stepIndex === CORE_STEPS.length;

    const refreshConfig = () => queryClient.invalidateQueries({ queryKey: queryKeys.config });
    const announce = (message) => { if (isCurrentView()) setStatusMessage(message); };
    const verifyStoredGoogle = async () => {
        setSaving(true);
        try {
            const result = await queryClient.fetchQuery({queryKey:['tutorialGoogleVerification', config?.revision], queryFn:welfareApi.verifyGoogleApiKey, staleTime:0});
            if (!isCurrentView()) return;
            announce(result.ok ? '저장된 AI 연결을 확인했습니다.' : '저장된 키의 연결을 확인하지 못했습니다. 키 또는 인터넷 연결을 확인해주세요.');
            if (result.ok) advanceAfterSave('google');
        } catch { announce('AI 연결 확인에 실패했습니다. 잠시 후 다시 시도해주세요.'); }
        finally { setSaving(false); }
    };

    const saveGoogle = async (event) => {
        event?.preventDefault();
        if (!googleKey.trim()) return toast.info('Google AI Studio에서 발급받은 Gemini API 키를 입력해주세요.');
        setSaving(true); announce('AI 연결을 확인하는 중입니다.');
        try {
            if (googleKey.includes('...') || /^[•*]+$/.test(googleKey.trim())) throw Error('가려진 키가 아닌 발급받은 전체 키를 입력해주세요.');
            await saveDraftConfig({ GOOGLE_API_KEY: googleKey.trim() });
            settingsDrafts.commit('apiKey', googleKey, '');
            await refreshConfig();
            const result = await queryClient.fetchQuery({
                queryKey: ['tutorialGoogleVerification', queryClient.getQueryData(queryKeys.config)?.revision],
                queryFn: welfareApi.verifyGoogleApiKey, staleTime: 5 * 60 * 1000,
            });
            queryClient.setQueryData(['tutorialGoogleVerification', result.revision], result);
            if (!result.ok) {
                announce('연결하지 못했습니다. 입력한 Gemini API 키와 인터넷 연결을 확인해주세요.');
                return saveToast.warning(result.error || 'Gemini API 키를 확인해주세요.');
            }
            announce('AI 연결이 완료되었습니다. 다음 단계로 이동할 수 있습니다.');
            saveToast.success('AI 연결을 완료했습니다.');
            advanceAfterSave('google');
        } catch (error) {
            announce('AI 연결 중 오류가 발생했습니다.');
            saveToast.error('AI 연결 실패: ' + getErrorMessage(error));
        } finally { setSaving(false); }
    };


    const saveNotionToken = async (event) => {
        event?.preventDefault();
        if (!notionToken.trim()) return toast.info('Notion에서 복사한 연결 토큰을 입력해주세요.');
        setSaving(true); announce('Notion 연결 토큰을 저장하는 중입니다.');
        try {
            await saveDraftConfig({ NOTION_TOKEN: notionToken.trim() });
            settingsDrafts.commit('notionToken', notionToken, '');
            await refreshConfig();
            announce('Notion 연결 토큰을 저장했습니다. 아래에서 이 기능에 필요한 표만 연결하세요.');
            saveToast.success('Notion 연결 토큰을 저장했습니다.');
            advanceAfterSave('notion');
        } catch (error) {
            announce('Notion 연결 토큰을 저장하지 못했습니다.');
            saveToast.error('Notion 연결 실패: ' + getErrorMessage(error));
        } finally { setSaving(false); }
    };

    const disconnectNewsGallery = async () => {
        if (saving || settingsDrafts.isWriting() || !config?.notion_configured || settingsDrafts.dirty('notionDbId')) return;
        if (!window.confirm('새 소식 보관함 연결을 해제할까요?\n앱의 연결만 해제하며, Notion 원본은 삭제하지 않습니다.\nNotion 연결 토큰과 정책 최신화 연결은 유지됩니다.')) return;
        setSaving(true);
        try {
            await saveDraftConfig({ NOTION_DATABASE_ID: '' });
            settingsDrafts.commit('notionDbId', newsDbId, '');
            queryClient.setQueryData(queryKeys.config, previous => previous ? {...previous, NOTION_DATABASE_ID: '', notion_configured: false} : previous);
            if (isCurrentView()) setEditing('news');
            await refreshConfig();
            announce('새 소식 보관함 연결을 해제했습니다.');
            saveToast.success('연결을 해제했습니다. Notion 원본은 그대로 있습니다.');
        } catch (error) {
            saveToast.error('연결 해제 상태를 확인하지 못했습니다: ' + getErrorMessage(error));
        } finally { if (viewRef.current.mounted) setSaving(false); }
    };

    const saveNewsGallery = async (event) => {
        event?.preventDefault();
        if (!newsDbId.trim()) return toast.info('복제한 새 소식 보관함의 전체 주소를 입력해주세요.');
        if (!config?.notion_token_configured && !notionToken.trim()) return toast.info('먼저 Notion 연결 토큰을 입력해주세요.');
        const payload = { NOTION_DATABASE_ID: newsDbId.trim() };
        if (!config?.notion_token_configured && notionToken.trim()) payload.NOTION_TOKEN = notionToken.trim();
        setSaving(true); announce('새 소식 보관함 연결을 확인하는 중입니다.');
        try {
            await saveDraftConfig(payload);
            if (payload.NOTION_TOKEN) settingsDrafts.commit('notionToken', notionToken, '');
            settingsDrafts.commit('notionDbId', newsDbId, payload.NOTION_DATABASE_ID);
            await refreshConfig();
            const result = await queryClient.fetchQuery({
                queryKey: ['tutorialNotionVerification', queryClient.getQueryData(queryKeys.config)?.revision],
                queryFn: welfareApi.verifyNotion, staleTime: 5 * 60 * 1000,
            });
            queryClient.setQueryData(['tutorialNotionVerification', result.revision], result);
            if (!result.ok) {
                announce('주소는 저장했지만 새 소식 보관함를 확인하지 못했습니다.');
                saveToast.warning('보관함의 연결 권한과 항목 구성을 확인해주세요: ' + result.error);
            } else {
                announce('새 소식 보관함 연결을 완료했습니다.');
                saveToast.success('새 소식 보관함 연결을 완료했습니다.');
                advanceAfterSave('news');
            }
        } catch (error) {
            announce('새 소식 보관함를 연결하지 못했습니다.');
            saveToast.error('새 소식 보관함 연결 실패: ' + getErrorMessage(error));
        } finally { setSaving(false); }
    };

    const savePolicyDbs = async (event) => {
        event?.preventDefault();
        if (!config?.notion_token_configured) return toast.info('먼저 이전 단계에서 Notion 연결 토큰을 저장해주세요.');
        const items = policyDbPayload(policyDbs);
        setSaving(true); announce('등록한 정책표의 연결과 항목 구성을 확인하는 중입니다.');
        try {
            await saveDraftConfig({ NOTION_POLICY_DBS: JSON.stringify(items) });
            settingsDrafts.commit('policyDbs', policyDbs, items.length ? items : [createPolicyDbDraft(0)]);
            await refreshConfig();
            if (!items.length) {
                announce('정책표 연결을 모두 해제했습니다. Notion 원본은 그대로 유지됩니다.');
                saveToast.success('정책표 연결을 모두 해제했습니다.');
                return;
            }
            let result = await queryClient.fetchQuery({
                queryKey: ['tutorialPolicyDbVerification', queryClient.getQueryData(queryKeys.config)?.revision],
                queryFn: welfareApi.verifyPolicyDbs, staleTime: 5 * 60 * 1000,
            });
            const titled = applyVerifiedPolicyTitles(items, result.items);
            const currentDraft = settingsDrafts.get('policyDbs');
            if (!settingsDrafts.dirty('policyDbs') && JSON.stringify(policyDbPayload(currentDraft)) === JSON.stringify(items) && JSON.stringify(titled) !== JSON.stringify(items)) {
                const saved = await saveDraftConfig({ NOTION_POLICY_DBS: JSON.stringify(titled) });
                result = { ...result, revision: saved.revision };
                settingsDrafts.commit('policyDbs', currentDraft, titled);
                await refreshConfig();
            }
            queryClient.setQueryData(['tutorialPolicyDbVerification', result.revision], result);
            if (result.ok) {
                announce('등록한 정책표가 모두 연결되었습니다.');
                saveToast.success('등록한 정책표의 연결을 확인했습니다.');
                advanceAfterSave('policy');
            } else {
                announce('확인이 필요한 정책표가 있습니다. 각 항목의 안내를 확인해주세요.');
                saveToast.warning('확인이 필요한 정책표가 있습니다.');
            }
        } catch (error) {
            announce('정책표를 확인하지 못했습니다.');
            saveToast.error('정책표 설정 실패: ' + getErrorMessage(error));
        } finally { setSaving(false); }
    };

    const clearPolicyVerification = () => queryClient.removeQueries({ queryKey: ['tutorialPolicyDbVerification'] });
    const addPolicyDb = () => {
        setPolicyDbs(rows => [...rows, createPolicyDbDraft(rows.length)]);
        clearPolicyVerification();
    };
    const removePolicyDb = key => {
        setPolicyDbs(rows => {
            const next = rows.filter(row => row.key !== key);
            return next.length ? next : [createPolicyDbDraft(0)];
        });
        clearPolicyVerification();
        announce('연결 목록에서 뺐습니다. 저장하면 앱 연결만 해제되고 Notion 원본은 유지됩니다.');
    };

    const goNext = () => {
        if (!done[current.key]) {
            toast.info('이 단계는 기본 기능에 필요합니다. 지금 건너뛰어도 나중에 다시 완료할 수 있습니다.');
        }
        setStepIndex(index => Math.min(CORE_STEPS.length, index + 1));
    };

    const featureSteps = featureSetupSteps(featureMode);
    const featureIndex = featureSteps.indexOf(activeSection);
    const nextFeatureSection = featureSteps[featureIndex + 1];
    const canContinue = activeSection === 'signup' || activeSection === 'template' || flowDone[activeSection];
    const continueFeature = () => {
        if (saving || checkingConnections || !canContinue) return;
        if (activeSection === 'signup') setAcknowledged(value => ({...value, signup: true}));
        if (activeSection === 'template') setAcknowledged(value => ({...value, [`${featureMode}-template`]: true}));
        setSelectedSection({mode:featureMode, key:nextFeatureSection || null});
    };
    return (
        <section ref={guideRef} className="bg-white dark:bg-gray-800">
            <p className="sr-only" aria-live="polite">{statusMessage}</p>
            <div data-setup-sticky className="sticky top-[73px] z-10 bg-white dark:bg-gray-800 px-4 sm:px-6 py-3 border-b border-gray-200 dark:border-gray-700">
                <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                        <h2 className="text-xl font-semibold leading-snug text-gray-900 dark:text-gray-100">{unified ? '처음 설정' : featureMode ? FEATURE_GUIDES[featureMode].title : '새 소식 탐색 준비'}</h2>
                        <p className="text-sm text-gray-500 mt-1">{unified ? '공통 연결을 준비한 뒤 템플릿을 선택하세요.' : featureMode ? FEATURE_GUIDES[featureMode].description : '처음 새 소식을 찾는 데 꼭 필요한 세 가지만 차례로 준비합니다.'}</p>
                    </div>
                </div>
                {!featureMode && <div className="sr-only" role="progressbar" aria-label="처음 설정 진행률" aria-valuemin="1" aria-valuemax={CORE_STEPS.length} aria-valuenow={isCompleteScreen ? CORE_STEPS.length : stepIndex + 1} aria-valuetext={`${isCompleteScreen ? '설정 결과' : `${stepIndex + 1}단계 ${current.label}`} · 기본 준비 ${progress.collectionCompleted}/${progress.collectionTotal}`} />}
                <SetupSteps
                    items={unified ? featureSetupItems('policy').map(item=>item.key==='policy'?{...item,key:'address'}:item) : featureMode ? featureSetupItems(featureMode) : CORE_STEPS}
                    done={unified ? {...done,template:Boolean(templateChoice&&done.template),address:Boolean(templateChoice&&done[templateChoice])} : done} activeKey={unified&&['policy','news'].includes(activeSection)?'address':featureMode ? activeSection : isCompleteScreen ? null : current.key}
                    disabled={saving || checkingConnections}
                    label={`${unified ? '처음 설정' : featureMode ? FEATURE_GUIDES[featureMode].title : '새 소식 탐색 준비'} 진행 현황`}
                    onSelect={key => featureMode ? setSelectedSection({mode:featureMode,key:unified&&key==='address'?(templateChoice||'template'):key}) : setStepIndex(CORE_STEPS.findIndex(step=>step.key===key))}
                />
            </div>

            <div className="p-4 sm:p-6 min-h-[300px]">
                {featureMode ? (
                    <div className="mx-auto max-w-3xl space-y-5">
                        {unified && <div className="flex flex-wrap justify-between gap-2 text-sm">
                            {templateChoice && <button type="button" disabled={saving||settingsDrafts.isWriting()} onClick={()=>{setTemplateChoice(null);setSelectedSection({mode:'policy',key:'template'});}} className="min-h-10 rounded-lg px-2 text-indigo-700 underline dark:text-indigo-300">템플릿 선택으로</button>}
                            <button type="button" disabled={saving||settingsDrafts.isWriting()} onClick={()=>navigate('/settings?tab=guide&step=target')} className="ml-auto min-h-10 rounded-lg px-2 text-gray-600 underline dark:text-gray-300">Notion은 나중에 설정 · 수집 준비하기</button>
                        </div>}
                        {!activeSection && !checkingConnections && (firstIncompleteSetupStep(featureMode, flowDone)
                            ? <p role="status" className="rounded-xl bg-gray-50 p-4 text-sm text-gray-700 dark:bg-gray-900 dark:text-gray-300">아직 준비할 연결이 있습니다. 위에서 설정할 항목을 선택하세요.</p>
                            : <p role="status" className="rounded-xl bg-emerald-50 p-4 text-sm text-emerald-800">필요한 연결을 준비했습니다. 아래 버튼으로 업무를 시작하거나 위 단계에서 설정 방법을 다시 볼 수 있습니다.</p>)}

                        {checkingConnections && <p role="status" className="text-sm text-gray-500">저장된 연결 상태를 확인하고 있습니다.</p>}
                        <SetupSection {...sectionProps('google')}>
                            <div className="flex items-center justify-between gap-3">
                                <div><h3 data-setup-heading className="text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">AI와 복지F5 연결</h3><p className="mt-2 text-sm text-gray-500">새 소식 탐색과 함께 쓰는 공통 연결입니다.</p></div>
                                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${done.google ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'}`}>{done.google ? '완료' : '설정 필요'}</span>
                            </div>
                            {done.google && <CompletedConnection title="AI 연결 완료" description="이미 연결된 Gemini API 키를 그대로 사용합니다. 다시 입력할 필요가 없습니다." editLabel="Gemini 연결·변경" onEdit={() => setEditing('google')} />}
                            {done.google && <details className="text-sm text-gray-600 dark:text-gray-300"><summary className="cursor-pointer">Gemini 설정 방법 다시 보기</summary><div className="mt-4"><SetupStageInstructions kind="google" /></div></details>}
                            {featureMode === 'news' && <p className="text-sm leading-6 text-gray-600 dark:text-gray-300">AI 분석을 사용할 때 연결하세요. 원문 수집과 Notion 보관만 이용한다면 아래에서 건너뛸 수 있습니다.</p>}
                            {!done.google && <>
                                <SetupStageInstructions kind="google" />
                                <form onSubmit={saveGoogle} className="mt-6 space-y-4">
                                    <div><label htmlFor="policy-google-key" className="block mb-1.5 text-sm font-medium">Gemini API 키 <span className="font-normal text-gray-400">(AI 연결 키)</span></label><input id="policy-google-key" type="password" autoComplete="off" value={googleKey} onChange={e => setGoogleKey(e.target.value)} placeholder="Google AI Studio에서 복사한 키 전체를 붙여넣으세요" className={inputClass} /></div>
                                    <button type="submit" disabled={saving} className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{saving && <Loader2 className="h-4 w-4 animate-spin" />} 저장하고 연결 확인</button>
                                </form>
                            </>}
                        </SetupSection>

                        <SetupSection {...sectionProps('signup')}>
                            <h3 data-setup-heading className="text-base font-semibold">Notion 가입·로그인</h3>
                            <p className="text-sm leading-6 text-gray-600 dark:text-gray-300">Notion은 정책표와 새 소식을 정리해 보관하는 공간입니다. 정책 최신화와 새 소식의 Notion 저장을 이용하려면 연결이 필요합니다. 새 소식 수집과 소식 모음 파일 저장은 Notion 없이도 사용할 수 있습니다.</p>
                            <SetupStageInstructions kind="notion" phase="signup" />
                            <p className="text-sm text-gray-500">이미 계정이 있다면 새로 가입하지 마세요. 사용할 작업 공간을 확인한 뒤 아래에서 계속하세요.</p>
                        </SetupSection>

                        <SetupSection {...sectionProps('notion')}>
                            <div className="flex items-center justify-between gap-3">
                                <div><h3 data-setup-heading className="text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">Notion과 복지F5 연결</h3><p className="mt-2 text-sm text-gray-500">정책표와 새 소식 보관함이 함께 쓰는 공통 연결입니다.</p></div>
                                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${done.notion ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'}`}>{done.notion ? '토큰 저장됨' : '설정 필요'}</span>
                            </div>
                            {done.notion && <CompletedConnection title="Notion 토큰 저장됨" description="저장된 토큰을 함께 사용합니다. 표에 접근할 수 있는지는 주소 등록 단계에서 확인합니다." editLabel="Notion 연결 토큰 변경" onEdit={() => setEditing('notion')} />}
                            <SetupStageInstructions kind="notion" />
                            {!done.notion && <form onSubmit={saveNotionToken} className="space-y-3">
                                <div><label htmlFor="onboarding-notion-token" className="block mb-1.5 text-sm font-medium">Notion 연결 토큰 <span className="font-normal text-gray-400">(Installation access token)</span></label><input id="onboarding-notion-token" type="password" autoComplete="off" value={notionToken} onChange={e => setNotionToken(e.target.value)} placeholder="Notion에서 복사한 토큰 전체를 붙여넣으세요" className={inputClass} /></div>
                                <button type="submit" disabled={saving} className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{saving && <Loader2 className="h-4 w-4 animate-spin" />} 연결 토큰 저장</button>
                            </form>}
                        </SetupSection>

                        <SetupSection {...sectionProps('template')}>
                            <h3 data-setup-heading className="text-base font-semibold">{unified&&!templateChoice?'복제할 템플릿 선택':`${featureMode === 'news' ? '새 소식 보관함' : '정책표'} 복제와 연결 권한`}</h3>
                            {unified&&!templateChoice ? <SetupTemplateChooser done={done} disabled={saving||checkingConnections} onSelect={key=>{setTemplateChoice(key);setSelectedSection({mode:key,key:'template'});}}/> : <>
                                <SetupStageInstructions kind={featureMode} phase="template" templateHref={featureMode === 'news' ? NOTION_NEWS_TEMPLATE_URL : NOTION_TEMPLATE_URL} />
                                <p className="text-sm text-gray-500">복제와 권한 설정을 마친 뒤 주소를 등록하세요.</p>
                            </>}
                        </SetupSection>

                        {featureMode === 'news' ? (
                            <SetupSection {...sectionProps('news')}>
                                <div className="flex items-center justify-between gap-3">
                                    <div><h3 data-setup-heading className="text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">새 소식 보관함 연결</h3></div>
                                    <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${newsGalleryReady ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'}`}>{newsGalleryReady ? '완료' : '선택 설정'}</span>
                                </div>
                                <p className="text-sm leading-6 text-gray-600 dark:text-gray-300"><b>Notion에 카드로 보관할 때만</b> 진행하세요. 소식 모음 파일은 이 설정 없이 바로 저장할 수 있습니다.</p>
                                {!showNewsForm && <><CompletedConnection title="새 소식 보관함 연결 완료" description="새 소식 화면에서 확인한 항목을 이 보관함에 등록할 수 있습니다." editLabel="새 소식 보관함 연결·변경" onEdit={() => setEditing('news')} /><p className="break-all rounded-xl bg-gray-50 p-4 text-sm dark:bg-gray-900"><span className="block font-semibold">등록된 보관함 주소 또는 ID</span>{newsDbId}</p></>}
                                {config?.notion_configured && <button type="button" onClick={disconnectNewsGallery} disabled={saving || settingsDrafts.isWriting() || settingsDrafts.dirty('notionDbId')} className="min-h-9 rounded-lg px-2 py-1 text-xs text-gray-500 underline underline-offset-4 hover:bg-gray-50 hover:text-gray-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200">새 소식 보관함 연결 해제</button>}
                                <SetupStageInstructions kind="news" phase="register" />
                                {showNewsForm && <form onSubmit={saveNewsGallery} className="space-y-3">
                                    <label htmlFor="onboarding-news-db" className="block text-sm font-semibold">내 새 소식 보관함 주소</label>
                                    <input id="onboarding-news-db" value={newsDbId} onChange={e => setNewsDbId(e.target.value)} placeholder="복제한 Notion 보관함의 전체 주소를 붙여넣으세요" className={inputClass} />
                                    <p className="text-xs leading-5 text-gray-500">필수 항목: 이름(제목) · 기관(텍스트) · 선택(선택) · 기간(날짜). 예시 카드는 연결 확인 뒤 삭제해도 됩니다.</p>
                                    {!config?.notion_token_configured && !notionToken.trim() && <p className="text-xs font-medium text-amber-700 dark:text-amber-300">먼저 3단계 Notion API에서 연결 토큰을 저장하세요.</p>}
                                    <button type="submit" disabled={saving || (!config?.notion_token_configured && !notionToken.trim())} className="rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">저장하고 보관함 연결 확인</button>
                                </form>}
                            </SetupSection>
                        ) : (
                            <SetupSection {...sectionProps('policy')}>
                                <div className="flex items-center justify-between gap-3">
                                    <div><h3 data-setup-heading className="text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">정책표 연결</h3></div>
                                    <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold ${done.policy ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300' : 'bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'}`}>{done.policy ? '완료' : '설정 필요'}</span>
                                </div>
                                {done.policy && <><CompletedConnection title="정책표 연결 완료" description="정책표가 비어 있다면 정책 최신화 화면에서 첫 정책 초안을 추가하세요. 이미 정책이 있다면 최신 정보 점검을 시작하세요." editLabel="정책표 연결·변경" onEdit={() => setEditing('policy')} /><ul className="space-y-2">{policyDbs.map(db => <li key={db.key} className="break-all rounded-xl bg-gray-50 p-3 text-sm dark:bg-gray-900"><strong className="block">{db.name}</strong>{db.data_source_id}</li>)}</ul></>}
                                <SetupStageInstructions kind="policy" phase="register" />
                                <div data-policy-schema-note className="flex items-start gap-3 rounded-xl border border-blue-100 bg-blue-50/60 p-4 text-sm leading-relaxed text-gray-700 dark:border-blue-900/50 dark:bg-blue-950/20 dark:text-gray-200">
                                    <Info aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-blue-600 dark:text-blue-400"/>
                                    <div><p>페이지 제목과 선택지는 기관에 맞게 바꿀 수 있습니다.</p><p className="mt-1"><strong className="font-semibold">연결에 필요한 15개 항목(열)의 이름과 종류는 유지해주세요.</strong></p></div>
                                </div>
                                {!done.policy && <form onSubmit={savePolicyDbs} className="space-y-3"><div className="grid gap-3 lg:grid-cols-2">{policyDbs.map((db, index) => {
                                    const result = effectivePolicyCheck?.items?.find(item => item.key === db.key);
                                    const inputId = `onboarding-policy-${db.key}`;
                                    const optionTotal = result?.option_counts ? Object.values(result.option_counts).reduce((sum, count) => sum + Number(count || 0), 0) : 0;
                                    return <div key={db.key} className="rounded-xl border border-gray-100 p-3 dark:border-gray-700"><div className="mb-2 flex items-center justify-between gap-2"><label htmlFor={inputId} className="text-sm font-semibold">{result?.title || db.name || `정책표 ${index + 1}`}</label><div className="flex items-center gap-2">{result && <span className={`text-xs ${result.ok ? 'text-emerald-600' : 'text-amber-600'}`}>{result.ok ? '연결됨' : '확인 필요'}</span>}<button type="button" onClick={() => removePolicyDb(db.key)} className="inline-flex min-h-9 min-w-9 items-center justify-center rounded-lg text-gray-400 hover:bg-red-50 hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500" aria-label={`${db.name || `정책표 ${index + 1}`} 연결 삭제`}><Trash2 className="h-4 w-4" /></button></div></div><input id={inputId} value={db.data_source_id || ''} onChange={e => { setPolicyDbs(rows => rows.map(row => row.key === db.key ? { ...row, data_source_id: e.target.value } : row)); clearPolicyVerification(); }} placeholder="Notion 정책표의 전체 주소를 붙여넣으세요" className={inputClass} />{result?.ok && <p className="mt-2 text-sm text-emerald-700">기관별 선택지 {optionTotal}개 확인 · 정책 점검에 자동 반영</p>}{result && !result.ok && <p className="mt-2 text-sm text-amber-700">{result.error || (result.missing_properties?.length ? `빠진 열(항목): ${result.missing_properties.join(', ')}` : result.type_mismatches?.length ? `형식을 확인할 열: ${result.type_mismatches.map(item => item.name).join(', ')}` : '표의 열(항목) 구성을 확인해주세요.')}</p>}</div>;
                                })}</div>
                                    <button type="button" onClick={addPolicyDb} className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-emerald-300 px-4 py-2.5 text-sm font-semibold text-emerald-700 hover:bg-emerald-50 dark:border-emerald-700 dark:text-emerald-300 dark:hover:bg-emerald-950/20"><Plus className="h-4 w-4" /> 정책표 연결 추가</button>
                                    <p className="text-xs leading-5 text-gray-500">여러 기관이나 분야의 정책표를 필요한 만큼 추가할 수 있습니다. 휴지통은 앱 연결만 해제하며 Notion 원본은 삭제하지 않습니다.</p>
                                    {!config?.notion_token_configured && <p className="text-xs font-medium text-amber-700 dark:text-amber-300">먼저 Notion 연결 단계에서 토큰을 저장하세요.</p>}
                                    <button type="submit" disabled={saving || !config?.notion_token_configured} className="inline-flex items-center gap-2 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">{saving && <Loader2 className="h-4 w-4 animate-spin" />} 저장하고 정책표 연결 확인</button>
                                </form>}
                            </SetupSection>
                        )}
                        {statusMessage && <div role="status" className={`rounded-xl border px-4 py-3 text-sm font-medium ${statusMessage.includes('완료') || statusMessage.includes('저장했습니다') || statusMessage.includes('연결되었습니다') ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : saving ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-amber-200 bg-amber-50 text-amber-700'}`}>{saving && <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />}{statusMessage}</div>}
                        {activeSection && <div className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 pt-4 dark:border-gray-700">
                            <button type="button" disabled={saving || checkingConnections || featureIndex <= 0} onClick={() => setSelectedSection({mode:featureMode,key:featureSteps[featureIndex - 1]})} className="min-h-11 rounded-xl border px-4 py-2 text-sm disabled:opacity-40">← 이전</button>
                            {featureMode === 'news' && activeSection === 'google' && !done.google && <button type="button" disabled={saving || checkingConnections || settingsDrafts.dirty('apiKey')} onClick={() => {setSkipNewsAi(true);setSelectedSection({mode:featureMode,key:'signup'});}} className="min-h-11 rounded-xl border px-4 py-2 text-sm disabled:opacity-40">AI는 나중에 연결</button>}
                            {nextFeatureSection && !(unified&&!templateChoice&&activeSection==='template') && <button type="button" disabled={saving || checkingConnections || !canContinue} onClick={continueFeature} className="min-h-11 rounded-xl bg-indigo-600 px-5 py-2 text-sm font-semibold text-white disabled:opacity-40">{activeSection === 'signup' ? '작업 공간 확인했어요 · 다음' : activeSection === 'template' ? '복제·권한 설정했어요 · 다음' : '다음'} →</button>}
                        </div>}
                        <div className="flex flex-col gap-2 border-t border-gray-100 pt-4 sm:flex-row sm:justify-between dark:border-gray-700">
                            <Link to="/settings?tab=guide" className="inline-flex min-h-10 items-center justify-center gap-1 rounded-xl border border-gray-200 px-4 py-2 text-sm font-semibold text-gray-700 dark:border-gray-700 dark:text-gray-200"><ArrowLeft className="h-4 w-4" /> 설정 안내 목록으로</Link>
                            {!unified && !firstIncompleteSetupStep(featureMode, flowDone) && <Link to={featureMode === 'policy' ? '/policy' : '/notion'} className="inline-flex min-h-10 items-center justify-center gap-1 rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white">{featureMode === 'policy' ? '정책 최신화 화면' : '새 소식 화면'}으로 이동 <ArrowRight className="h-4 w-4" /></Link>}
                            {!unified && featureMode === 'news' && <Link to="/settings?tab=guide&step=target" className="min-h-10 px-2 py-2 text-sm text-blue-700 underline dark:text-blue-300">Notion 없이 수집 준비하기</Link>}
                        </div>
                    </div>
                ) : isCompleteScreen ? (
                    <div className="max-w-2xl mx-auto text-center py-4 sm:py-8">
                        <div className={`w-16 h-16 mx-auto rounded-2xl flex items-center justify-center ${progress.collectionReady ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600'}`}><CheckCircle2 className="w-9 h-9" /></div>
                        <h3 className="mt-5 text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">{progress.collectionReady ? '정보 수집 준비를 마쳤습니다' : '정보 수집에 필요한 설정을 확인해주세요'}</h3>
                        <p className="mt-2 text-sm text-gray-500">{progress.collectionReady ? '이제 새 소식 화면에서 첫 수집을 실행해 결과를 확인할 차례입니다.' : '아래 세 항목을 완료하면 정보 수집을 시작할 수 있습니다.'}</p>
                        <p className="mt-5 text-left text-sm font-medium text-gray-800 dark:text-gray-100">정보 수집 준비</p>
                        {done.target && !defaultReadiness.data?.enabled && <p role="status" className="mt-3 text-left text-sm text-amber-700 dark:text-amber-300">{defaultReadiness.data?.reason || '수집 실행 준비 상태를 확인해야 합니다. 새 소식 화면에서 다시 확인해주세요.'}</p>}
                        <div className="mt-2 grid gap-2 text-left">
                            {CORE_STEPS.map((step, index) => (
                                <button type="button" key={step.key} onClick={() => setStepIndex(index)} className="flex items-center justify-between rounded-xl border border-gray-100 dark:border-gray-700 px-4 py-3 text-sm">
                                    <span>{step.label}</span><span className={done[step.key] ? 'text-emerald-600 font-medium' : 'text-amber-600 font-medium'}>{done[step.key] ? '완료' : '설정 필요'}</span>
                                </button>
                            ))}
                        </div>
                        <p className="mt-6 text-left text-sm font-medium text-gray-800 dark:text-gray-100">필요한 기능만 추가로 준비하세요 <span className="font-normal text-gray-400">(선택)</span></p>
                        <div className="mt-2 grid gap-3 text-left sm:grid-cols-2">
                            <Link to="/settings?tab=guide&step=policy" className="rounded-2xl border border-emerald-100 bg-emerald-50/40 p-4 hover:border-emerald-300 dark:border-emerald-900/40 dark:bg-emerald-950/10">
                                <span className="flex items-center justify-between gap-2"><b className="text-sm text-gray-900 dark:text-gray-100">정책 최신화</b><span className={`text-xs font-semibold ${progress.policyReady ? 'text-emerald-600' : 'text-gray-500'}`}>{progress.policyReady ? '준비 완료' : `${progress.policyCompleted}/${progress.policyTotal} 완료`}</span></span>
                                <span className="mt-2 block text-xs leading-5 text-gray-500">Notion 정책의 변경 내용을 점검할 때 준비합니다. 새 소식 보관함은 필요 없습니다.</span>
                                <span className="mt-3 inline-flex items-center gap-1 text-sm font-semibold text-emerald-700 dark:text-emerald-300">준비 방법 보기 <ArrowRight className="h-4 w-4" /></span>
                            </Link>
                            <Link to="/settings?tab=guide&step=news" className="rounded-2xl border border-blue-100 bg-blue-50/40 p-4 hover:border-blue-300 dark:border-blue-900/40 dark:bg-blue-950/10">
                                <span className="flex items-center justify-between gap-2"><b className="text-sm text-gray-900 dark:text-gray-100">새 소식 Notion 저장</b><span className={`text-xs font-semibold ${progress.newsReady ? 'text-emerald-600' : 'text-gray-500'}`}>{progress.newsReady ? '준비 완료' : `${progress.newsCompleted}/2 완료`}</span></span>
                                <span className="mt-2 block text-xs leading-5 text-gray-500">찾은 새 소식을 Notion 카드로 보관할 때만 준비합니다. 소식 모음 파일 저장에는 필요 없습니다.</span>
                                <span className="mt-3 inline-flex items-center gap-1 text-sm font-semibold text-blue-700 dark:text-blue-300">준비 방법 보기 <ArrowRight className="h-4 w-4" /></span>
                            </Link>
                        </div>
                        <div className="mt-6 flex flex-col sm:flex-row justify-center gap-2">
                            {progress.collectionReady ? (
                                <Link to="/notion" className="inline-flex items-center justify-center gap-2 rounded-xl bg-indigo-600 px-5 py-3 text-sm font-semibold text-white">새 소식 찾으러 가기 <ArrowRight className="w-4 h-4" /></Link>
                            ) : (
                                <span aria-disabled="true" className="inline-flex cursor-not-allowed items-center justify-center gap-2 rounded-xl bg-gray-200 px-5 py-3 text-sm font-semibold text-gray-500">새 소식 찾으러 가기 <ArrowRight className="w-4 h-4" /></span>
                            )}
                            <button type="button" onClick={() => setStepIndex(0)} className="px-5 py-3 rounded-xl border border-gray-200 dark:border-gray-700 text-sm font-semibold">설정 방법 다시 보기</button>
                        </div>
                        <p className="mt-4 text-sm text-gray-500">추가 준비는 지금 모두 하지 않아도 됩니다. 해당 기능을 처음 사용할 때 이어서 설정할 수 있습니다.</p>
                    </div>
                ) : <>
                    <div className="flex items-center gap-3 mb-6">
                        <div className="min-w-0"><h3 data-setup-heading className="text-base font-semibold leading-snug text-gray-900 dark:text-gray-100">{current.label}</h3><p className="mt-2 text-sm text-gray-500">{current.key === 'google' ? '정책 최신화와 함께 쓰는 공통 연결입니다.' : done[current.key] ? '새 소식을 찾는 데 필요한 설정을 완료했습니다.' : '처음 새 소식을 찾기 위해 필요한 단계입니다.'}</p></div>
                    </div>

                    {current.key === 'google' && <div className="space-y-6 max-w-3xl">
                        {done.google && <CompletedConnection title="AI 연결 완료" description="연결이 끝나도 아래에서 키 발급 방법을 언제든 다시 볼 수 있습니다." editLabel="Gemini 연결·변경" onEdit={() => setEditing('google')} />}
                        <SetupWalkthrough kind="google" completed={done.google} key={`google-${done.google}`} inputId="onboarding-google-key" />
                        {!done.google && <form onSubmit={saveGoogle} className="mt-6 space-y-4">
                            {config?.is_configured && <div className="mb-6 space-y-2"><p className="text-sm text-gray-500">키가 저장되어 있습니다. 그대로 사용하려면 연결만 확인하세요.</p><button type="button" onClick={verifyStoredGoogle} disabled={saving || settingsDrafts.dirty('apiKey')} className="rounded-xl border px-4 py-2.5 text-sm disabled:opacity-50">기존 키 연결 확인</button></div>}
                            <div><label htmlFor="onboarding-google-key" className="block mb-1.5 text-sm font-medium">{config?.is_configured ? '새 Gemini API 키 (변경할 때만 입력)' : 'Gemini API 키'} <span className="text-gray-400 font-normal">(AI 연결 키)</span></label><input id="onboarding-google-key" type="password" autoComplete="off" value={googleKey} onChange={e => setGoogleKey(e.target.value)} placeholder="Google AI Studio에서 복사한 키 전체를 붙여넣으세요" className={inputClass} /></div>
                            <button type="submit" disabled={saving || !googleKey.trim()} className="px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold disabled:opacity-50 inline-flex items-center gap-2">{saving && <Loader2 className="w-4 h-4 animate-spin" />} {config?.is_configured ? '새 키 저장하고 연결 확인' : '저장하고 연결 확인'}</button>
                        </form>}
                    </div>}

                    {current.key === 'target' && <DefaultNewsConditionEditor onSaved={() => setStepIndex(2)} />}

                    {current.key === 'sites' && <div className="space-y-4 max-w-2xl">
                        <ObjectiveSteps items={['사이트 등록 화면을 엽니다.', '복지정보를 가져올 기관 홈페이지 주소를 입력합니다.', '분석 결과를 확인하고 등록합니다.']} completion="등록된 홈페이지가 1개 이상 표시됩니다." />
                        <p className="text-sm font-medium">현재 등록된 홈페이지: <span className="text-blue-600">{sites?.length || 0}개</span></p>
                        <Link to="/sites?from=guide" onClick={() => window.localStorage.setItem(STEP_STORAGE_KEY, '2')} className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-600 text-white text-sm font-semibold">사이트 등록 화면 열기 <ArrowRight className="w-4 h-4" /></Link>
                    </div>}

                    {statusMessage && <div role="status" className={`mt-5 rounded-xl border px-4 py-3 text-sm font-medium ${statusMessage.includes('완료') || statusMessage.includes('저장했습니다') || statusMessage.includes('연결되었습니다') ? 'border-emerald-200 bg-emerald-50 text-emerald-700' : saving ? 'border-blue-200 bg-blue-50 text-blue-700' : 'border-amber-200 bg-amber-50 text-amber-700'}`}>{saving && <Loader2 className="mr-2 inline h-4 w-4 animate-spin" />}{statusMessage}</div>}
                </>}
            </div>

            {!featureMode && !isCompleteScreen && <div className="px-4 sm:px-6 py-4 border-t border-gray-100 dark:border-gray-700 flex items-center justify-between gap-3">
                <button type="button" onClick={() => setStepIndex(i => Math.max(0, i - 1))} disabled={stepIndex === 0} className="inline-flex min-h-10 items-center gap-1 rounded-lg px-3 text-sm text-gray-500 disabled:opacity-30"><ArrowLeft className="w-4 h-4" /> 이전</button>
                <button type="button" onClick={goNext} className={`inline-flex min-h-10 items-center gap-1 px-4 py-2 rounded-xl text-sm font-semibold ${done[current.key] ? 'bg-indigo-600 text-white' : 'border border-amber-200 bg-amber-50 text-amber-700'}`}>{stepIndex === CORE_STEPS.length - 1 ? '설정 결과 보기' : done[current.key] ? '다음' : '나중에 설정'} <ArrowRight className="w-4 h-4" /></button>
            </div>}
        </section>
    );
};

export default GuidedSetup;
