import React from 'react';
import {ageHint} from '../utils/newsProfileForm';

export const conditionCardClass = 'min-w-0 rounded-xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-900';

export default function NewsConditionCardSummary({profile}) {
    const targets=[...(profile.life_cycles||[]),...(profile.characteristics||[])].join(' · ')||'대상 제한 없음';
    const age=profile.min_age_months==null&&profile.max_age_months==null?'나이 제한 없음':`${profile.min_age_months==null?'최소 나이 제한 없음':ageHint(profile.min_age_months)} ~ ${profile.max_age_months==null?'최대 나이 제한 없음':ageHint(profile.max_age_months)}`;
    return <span className="block min-w-0 break-words [overflow-wrap:anywhere]">
        <span data-profile-name className="block text-base font-semibold text-gray-900 dark:text-gray-100">{profile.protected?'이전에 보관한 수집 조건':profile.name}</span>
        <span className="mt-2 block text-sm leading-6 text-gray-600 dark:text-gray-300">{profile.region||'지역 미확인'} · {targets}</span>
        <span className="block text-sm leading-6 text-gray-600 dark:text-gray-300">{age}</span>
    </span>;
}
