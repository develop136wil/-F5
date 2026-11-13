import React, {useId} from 'react';
import {ChevronDown} from 'lucide-react';
import KeywordListEditor from './KeywordListEditor';

export function conditionDetailsSummary(value) {
    return [value.keywords?.length?`찾을 단어 ${value.keywords.length}개`:null,
        value.excluded_keywords?.length?`제외할 단어 ${value.excluded_keywords.length}개`:null,
        value.ai_prompt?.trim()?'AI 요청 설정됨':null].filter(Boolean).join(' · ') || '추가 기준 없음';
}

export default function NewsConditionDetails({value,onChange,defaultOpen=false}) {
    const id=useId();
    return <details className="ui-disclosure" open={defaultOpen||undefined}>
        <summary className="flex items-center justify-between gap-4">
            <span className="min-w-0 space-y-1">
                <span className="ui-subtitle block">수집 세부 설정</span>
                <span className="ui-description block">찾을 단어 · 제외할 단어 · AI 요청</span>
                <span className="ui-meta block">{conditionDetailsSummary(value)}</span>
            </span>
            <ChevronDown aria-hidden="true" size={20} className="disclosure-chevron"/>
        </summary>
        <div className="space-y-5 border-t border-gray-200 p-4 dark:border-gray-700">
            <KeywordListEditor id={`${id}-include`} label="찾을 단어" value={value.keywords} onChange={words=>onChange('keywords',words)} description="관련 소식을 찾고 판단할 때 참고합니다."/>
            <KeywordListEditor id={`${id}-exclude`} label="제외할 단어" value={value.excluded_keywords} onChange={words=>onChange('excluded_keywords',words)} description="제목에 해당 단어가 있는 소식을 제외합니다."/>
            {!!value.excluded_keywords?.length && <p className="text-xs leading-5 text-amber-800 dark:text-amber-300">‘공고’, ‘마감’처럼 넓은 단어를 제외하면 필요한 모집 소식도 빠질 수 있습니다. 제외 범위를 확인해주세요.</p>}
            <div className="space-y-2">
                <label htmlFor={`${id}-ai`} className="block text-sm font-semibold">AI 요청</label>
                <p id={`${id}-ai-hint`} className="ui-description">소식을 분석할 때 참고할 기준을 적어주세요. AI 요청은 반드시 지켜지는 제외 규칙은 아닙니다. 저장만으로 AI를 호출하지 않습니다.</p>
                <textarea id={`${id}-ai`} aria-describedby={`${id}-ai-hint`} rows={3} maxLength={10000} className="ui-field resize-y" value={value.ai_prompt||''} onChange={event=>onChange('ai_prompt',event.target.value)} placeholder="예: 신청 대상과 신청 방법을 쉽게 설명해주세요."/>
            </div>
            <p className="ui-meta">위의 지역·대상·나이와 함께 최종 저장됩니다.</p>
        </div>
    </details>;
}
