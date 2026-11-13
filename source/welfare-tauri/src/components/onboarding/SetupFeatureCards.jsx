import React, {useSyncExternalStore} from 'react';
import {Link} from 'react-router-dom';
import {useQuery} from '@tanstack/react-query';
import {welfareApi} from '../../api/welfare';
import {queryKeys} from '../../api/queryKeys';
import {currentVerification} from '../../utils/verification';
import {settingsDrafts} from '../../utils/settingsDrafts';

export default function SetupFeatureCards({active = true}) {
    useSyncExternalStore(settingsDrafts.subscribe, settingsDrafts.getVersion);
    const {data:config} = useQuery({queryKey:queryKeys.config,queryFn:welfareApi.getConfig});
    const google=useQuery({queryKey:['tutorialGoogleVerification',config?.revision],queryFn:welfareApi.verifyGoogleApiKey,enabled:active&&Boolean(config?.is_configured),staleTime:300000,retry:false});
    const policy=useQuery({queryKey:['tutorialPolicyDbVerification',config?.revision],queryFn:welfareApi.verifyPolicyDbs,enabled:active&&Boolean(config?.notion_token_configured&&config?.NOTION_POLICY_DBS),staleTime:300000,retry:false});
    const news=useQuery({queryKey:['tutorialNotionVerification',config?.revision],queryFn:welfareApi.verifyNotion,enabled:active&&Boolean(config?.notion_configured),staleTime:300000,retry:false});
    const common=Boolean(config?.is_configured&&config?.notion_token_configured&&currentVerification(config?.revision,google.data)?.ok)&&!google.isError&&!google.isFetching&&!settingsDrafts.dirty('apiKey')&&!settingsDrafts.dirty('notionToken');
    return <div role="group" aria-label="시작 안내 선택" className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {[
            {key:'policy',title:'정책 최신화',description:'정책표를 복제하고 주소를 연결합니다.',check:policy,field:'policyDbs'},
            {key:'news',title:'새 소식 탐색',description:'새 소식 보관함을 복제하고 주소를 연결합니다.',check:news,field:'notionDbId'},
        ].map(item=>{
            const complete=common&&!item.check.isError&&!item.check.isFetching&&Boolean(currentVerification(config?.revision,item.check.data)?.ok)&&!settingsDrafts.dirty(item.field);
            const checking=!config||google.isFetching||item.check.isFetching;
            return <Link key={item.key} to={`/settings?tab=guide&step=${item.key}`} className="flex min-h-36 flex-col items-start justify-between gap-3 rounded-2xl border border-indigo-200 bg-indigo-50/40 p-5 text-left hover:border-indigo-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-600 dark:border-indigo-800 dark:bg-indigo-950/20">
                <span className="text-lg font-semibold text-gray-900 dark:text-gray-100">{item.title}</span>
                <span className="text-sm text-gray-600 dark:text-gray-300">{item.description}</span>
                <span className={complete?'text-sm font-semibold text-emerald-700 dark:text-emerald-300':'text-sm font-medium text-indigo-700 dark:text-indigo-300'}>{complete?'✓ 설정 완료 · 다시 보기':checking?'연결 확인 중…':'설정하기 →'}</span>
            </Link>;
        })}
    </div>;
}
