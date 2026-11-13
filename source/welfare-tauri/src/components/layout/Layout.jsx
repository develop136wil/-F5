import React, { useState, useEffect, useRef } from 'react';
import { NavLink } from 'react-router-dom';
import { Search, Settings, Home, RefreshCw, Square, Newspaper, RefreshCcwDot } from 'lucide-react';
import { cn } from '../../lib/utils';
import { useScrape } from '../../context/ScrapeContext';
import AiUsageStatus from '../AiUsageStatus';
import PolicyTaskStatus from './PolicyTaskStatus';
import NewsTaskProgress from '../NewsTaskProgress';
import { version as appVersion } from '../../../package.json';
import MenuToggleIcon from './MenuToggleIcon';
import { useQuery } from '@tanstack/react-query';
import {useAiStatus} from '../../hooks/useAiStatus';
import {useNewsTaskStatus} from '../../hooks/useNewsTaskStatus';
import {aiUsagePresentation} from '../../utils/aiUsagePresentation';

const SidebarItem = ({ to, icon: Icon, label, onClick }) => {
    const [tooltipDismissed, setTooltipDismissed] = useState(false);
    return (
        <NavLink
            to={to}
            aria-label={label}
            title={label}
            onClick={onClick}
            onFocus={() => setTooltipDismissed(false)}
            onMouseEnter={() => setTooltipDismissed(false)}
            onKeyDown={event => { if (event.key === 'Escape') setTooltipDismissed(true); }}
            className={({ isActive }) =>
                cn(
                    "group/item relative flex min-h-12 group-data-[collapsed=true]:md:min-h-[48px] items-center rounded-xl transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500",
                    isActive
                        ? "bg-primary/10 text-primary font-semibold dark:bg-blue-950 dark:text-blue-300"
                        : "text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-text dark:hover:text-gray-200"
                )
            }
        >
            <span className="flex h-12 group-data-[collapsed=true]:md:h-[48px] w-14 shrink-0 items-center justify-center"><Icon aria-hidden="true" size={20} /></span>
            <span className="pr-3 text-sm group-data-[collapsed=true]:md:hidden">{label}</span>
            <span aria-hidden="true" style={{visibility: tooltipDismissed ? 'hidden' : undefined}} className="pointer-events-none absolute left-full z-50 ml-3 hidden whitespace-nowrap rounded-lg bg-gray-800 px-3 py-2 text-sm text-white opacity-0 group-data-[collapsed=true]:md:block group-hover/item:opacity-100 group-focus-visible/item:opacity-100">{label}</span>
        </NavLink>
    );
};

const Layout = ({ children }) => {
    const { isScraping, isStopping, progress, statusMessage, stopScrape } = useScrape();
    const [menuOpen, setMenuOpen] = useState(false);
    const [collapsed,setCollapsed] = useState(()=>{try{return localStorage.getItem('sidebar-collapsed')==='true';}catch{return false;}});
    const [statusOpen,setStatusOpen] = useState(false);
    const navigationRef = useRef(null);
    const mobileToggleRef = useRef(null);
    const desktopToggleRef = useRef(null);
    const mainRef = useRef(null);
    const [toggleHintDismissed, setToggleHintDismissed] = useState(false);

    useEffect(() => {
        const desktop = window.matchMedia('(min-width: 768px)');
        const closeMobile = () => { if (desktop.matches) setMenuOpen(false); };
        desktop.addEventListener('change', closeMobile);
        return () => desktop.removeEventListener('change', closeMobile);
    }, []);

    useEffect(() => {
        if (!menuOpen) return;
        const navigation = navigationRef.current;
        const focusable = () => [...navigation.querySelectorAll('a[href], button:not(:disabled), [tabindex="0"]')]
            .filter(element => element.getClientRects().length && !element.closest('[inert]'));
        focusable()[0]?.focus();
        const keydown = event => {
            if (document.querySelector('dialog[open]')) return;
            if (event.key === 'Escape') {
                event.preventDefault();
                setMenuOpen(false);
            } else if (event.key === 'Tab') {
                const items = focusable();
                const first = items[0], last = items.at(-1);
                if (!items.length) { event.preventDefault(); return; }
                if (!navigation.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
                    event.preventDefault();
                    (event.shiftKey ? last : first).focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
        };
        document.addEventListener('keydown', keydown);
        return () => {
            document.removeEventListener('keydown', keydown);
            const target = mobileToggleRef.current?.getClientRects().length ? mobileToggleRef.current : desktopToggleRef.current;
            target?.focus();
        };
    }, [menuOpen]);
    const policyStatus=useQuery({queryKey:['global-policy-task-status'],enabled:false});
    const ai=useAiStatus(), news=useNewsTaskStatus();
    const aiView=aiUsagePresentation(ai);
    const activeTask=isScraping || policyStatus.data?.some(task=>task.running) || ai.data?.busy || news.data?.some(task=>task.active||['running','ready'].includes(task.state));
    const attention=policyStatus.isError||news.isError||aiView.tone==='warning'||news.data?.some(task=>['waiting','failed'].includes(task.state));
    const toggleSidebar=()=>{setCollapsed(value=>{try{localStorage.setItem('sidebar-collapsed',String(!value));}catch{}return !value;});setStatusOpen(false);};

    return (
        <div className="flex h-screen min-w-0 bg-[#F2F4F6] dark:bg-gray-900">
            <a href="#main-content" tabIndex={menuOpen ? -1 : 0} aria-hidden={menuOpen || undefined}
                onClick={event => { event.preventDefault(); mainRef.current?.focus(); }}
                className="fixed left-4 top-4 z-50 -translate-y-24 rounded-xl bg-blue-700 px-4 py-3 font-semibold text-white focus:translate-y-0 focus-visible:outline-white">본문으로 바로가기</a>
            <header inert={menuOpen ? '' : undefined} className="fixed inset-x-0 top-0 h-16 px-4 bg-white border-b border-gray-100 z-30 flex md:hidden items-center justify-between dark:bg-[#1a1d23] dark:border-gray-800">
                <span className="text-sm font-medium text-gray-500">메뉴</span>
                <button ref={mobileToggleRef} type="button" onClick={() => setMenuOpen(open => !open)} aria-expanded={menuOpen} aria-controls="mobile-navigation" aria-label={menuOpen ? '메뉴 닫기' : '메뉴 열기'} className="flex h-11 w-11 items-center justify-center rounded-lg text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"><MenuToggleIcon open={menuOpen}/></button>
            </header>
            {menuOpen && <button type="button" tabIndex={-1} aria-hidden="true" onClick={() => setMenuOpen(false)} className="fixed inset-0 bg-black/30 z-20 md:hidden" />}
            {/* Sidebar */}
            <aside ref={navigationRef} id="mobile-navigation" role={menuOpen ? 'dialog' : undefined} aria-modal={menuOpen || undefined} aria-label="메뉴" data-collapsed={collapsed} className={`group ${menuOpen ? 'flex' : 'hidden'} ${!collapsed || menuOpen ? 'overflow-y-auto' : ''} md:flex fixed md:relative shrink-0 inset-y-0 left-0 w-[260px] ${collapsed?'md:w-20':'md:w-[260px]'} max-w-[85vw] bg-white border-r border-gray-100 dark:border-gray-800 flex-col px-3 py-6 shadow-[4px_0_24px_rgba(0,0,0,0.08)] z-30 dark:bg-[#1a1d23]`}>
                {/* Icon-only rows keep comfortable fixed targets when only text is enlarged. */}
                <div className="h-16 group-data-[collapsed=true]:md:h-[64px] shrink-0">
                    <button ref={desktopToggleRef} type="button" onClick={toggleSidebar} onFocus={()=>setToggleHintDismissed(false)} onMouseEnter={()=>setToggleHintDismissed(false)} onKeyDown={event=>{if(event.key==='Escape')setToggleHintDismissed(true);}} aria-expanded={!collapsed} aria-controls="sidebar-links" title={collapsed?'메뉴 펼치기':'메뉴 접기'} aria-label={collapsed?'메뉴 펼치기':'메뉴 접기'} className="group/toggle relative ml-1.5 hidden h-11 group-data-[collapsed=true]:md:h-[44px] w-11 items-center justify-center rounded-xl text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 md:flex dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white">
                        <MenuToggleIcon open={!collapsed}/>
                        <span aria-hidden="true" style={{visibility:toggleHintDismissed?'hidden':undefined}} className="pointer-events-none absolute left-full z-50 ml-3 whitespace-nowrap rounded-lg bg-gray-800 px-3 py-2 text-sm text-white opacity-0 group-hover/toggle:opacity-100 group-focus-visible/toggle:opacity-100">{collapsed?'메뉴 펼치기':'메뉴 접기'}</span>
                    </button>
                    <button type="button" onClick={()=>setMenuOpen(false)} aria-label="메뉴 닫기" className="md:hidden flex h-14 w-14 items-center justify-center rounded-xl bg-slate-50 text-gray-600"><MenuToggleIcon open /></button>
                </div>

                <nav id="sidebar-links" aria-label="주요 메뉴" className="flex-1 space-y-1">
                    <SidebarItem to="/" icon={Home} label="홈" onClick={() => setMenuOpen(false)} />
                    <SidebarItem to="/policy" icon={RefreshCcwDot} label="정책 최신화" onClick={() => setMenuOpen(false)} />
                    <SidebarItem to="/notion" icon={Newspaper} label="새 소식" onClick={() => setMenuOpen(false)} />
                    <SidebarItem to="/search" icon={Search} label="통합 검색" onClick={() => setMenuOpen(false)} />
                    <SidebarItem to="/settings" icon={Settings} label="설정" onClick={() => setMenuOpen(false)} />
                </nav>

                {collapsed && <button type="button" aria-label={attention?'확인 필요 · 진행 상황':activeTask?'작업 진행 중 · 진행 상황':'진행 상황'} title={attention?'확인 필요 · 진행 상황':activeTask?'작업 진행 중 · 진행 상황':'진행 상황'} aria-expanded={statusOpen} aria-controls="sidebar-status" onClick={()=>setStatusOpen(value=>!value)} className="hidden md:flex min-h-[48px] shrink-0 items-center justify-center rounded-xl text-blue-600 hover:bg-blue-50">{attention?<span className="font-bold text-amber-700">!</span>:<RefreshCw size={20} className={activeTask?'animate-spin':''}/>}</button>}
                <div id="sidebar-status" onKeyDown={event=>{if(event.key==='Escape'){setStatusOpen(false);event.currentTarget.parentElement.querySelector('[aria-controls="sidebar-status"]')?.focus();}}} className={`mt-auto border-t border-gray-100 dark:border-gray-700 pt-4 space-y-3 ${collapsed ? statusOpen?'md:absolute md:left-full md:bottom-2 md:ml-2 md:w-72 md:max-h-[70vh] md:overflow-auto md:rounded-xl md:border md:bg-white md:p-4 md:shadow-lg dark:md:bg-gray-900':'md:hidden':''}`}>
                    <PolicyTaskStatus />
                    <AiUsageStatus />
                    {ai.data?.busy && aiView.tone!=='warning' && <p role="status" className="text-xs text-blue-700">{aiView.title}</p>}
                    {news.isError && <p role="status" className="text-xs text-amber-700">새 소식 진행 상태 확인 필요</p>}
                    {news.data?.map(task=><div key={task.workflow_id} title={task.profileName} className="space-y-2 rounded-lg bg-blue-50/60 p-3 text-xs dark:bg-blue-950/30"><NewsTaskProgress task={task} unavailable={news.isError} compact/></div>)}
                    {/* 전역 수집 인디케이터 — 어느 페이지에 있든 진행률/중지 노출 */}
                    {isScraping && (
                        <div className="rounded-xl border border-blue-100 dark:border-blue-900/40 bg-blue-50/60 dark:bg-blue-950/20 p-3 space-y-2">
                            <div className="flex items-center justify-between gap-2">
                                <span className="flex items-center gap-1.5 text-xs font-semibold text-blue-700 dark:text-blue-300">
                                    <RefreshCw size={12} className="animate-spin" /> 수집 중
                                </span>
                                <span className="text-xs font-bold text-blue-700 dark:text-blue-300">{progress}%</span>
                            </div>
                            <div className="w-full h-1.5 bg-blue-100 dark:bg-blue-900/40 rounded-full overflow-hidden">
                                <div className="h-full bg-blue-500 transition-all duration-500" style={{ width: `${progress}%` }} />
                            </div>
                            <p className="text-[11px] text-gray-500 dark:text-gray-400 truncate" title={statusMessage}>
                                {statusMessage || '진행 중...'}
                            </p>
                            <button
                                onClick={stopScrape}
                                disabled={isStopping}
                                className="w-full flex items-center justify-center gap-1.5 text-xs px-2 py-1.5 rounded-lg border border-red-200 dark:border-red-900/40 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/30 transition-colors"
                            >
                                <Square size={11} className="fill-current" /> 중지
                            </button>
                        </div>
                    )}
                    {/* (2026-07-21) 다크모드 일부 화면 색상 대비가 다듬어지지 않아, 제출 전까지
                        진입점만 숨겨 라이트 모드만 노출한다 — 로직·CSS는 그대로 남겨둬서
                        나중에 버튼 한 줄만 복구하면 다시 켤 수 있다. */}
                    <div className="px-4 py-2 text-xs text-gray-400">
                        v{appVersion}
                    </div>
                </div>
            </aside>

            {/* Main Content */}
            <main ref={mainRef} id="main-content" tabIndex={-1} inert={menuOpen ? '' : undefined} aria-label="본문" className="flex-1 min-w-0 overflow-auto scroll-pt-20 md:scroll-pt-8 px-4 pb-4 pt-20 md:p-8 dark:bg-gray-900 dark:text-gray-100">
                <div className="max-w-7xl mx-auto">
                    {children}
                </div>
            </main>
        </div>
    );
};

export default Layout;
