import React from 'react';
import {createRoot} from 'react-dom/client';
import {SetupStageInstructions} from '../../src/components/onboarding/SetupWalkthrough';
import {NOTION_TEMPLATE_URL,NOTION_NEWS_TEMPLATE_URL} from '../../src/constants/onboardingLinks';
import '../../src/index.css';
const q=new URLSearchParams(location.search),kind=q.get('kind')||'google';
createRoot(document.getElementById('root')).render(<div className="p-6 max-w-4xl"><SetupStageInstructions kind={kind} phase={q.get('phase')||undefined} templateHref={kind==='news'?NOTION_NEWS_TEMPLATE_URL:NOTION_TEMPLATE_URL}/></div>);
