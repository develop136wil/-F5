import React from 'react';

/**
 * 데이터가 없을 때 표시하는 공통 빈 상태 컴포넌트.
 * Home, Search, Sites, Settings 전반에 흩어진 빈 상태 UI를 통일합니다.
 *
 * @param {React.ComponentType} [icon] - lucide-react 아이콘 컴포넌트 (선택)
 * @param {string} [title] - 주 메시지
 * @param {string} [subtitle] - 부 설명 (선택)
 * @param {React.ReactNode} [children] - 하단 추가 콘텐츠 (버튼 등) (선택)
 * @param {string} [className] - 추가 CSS 클래스
 *
 * @example
 * <EmptyState
 *   icon={Globe}
 *   title="등록된 사이트가 없습니다"
 *   subtitle="URL을 입력하여 사이트를 추가하세요"
 * />
 */
const EmptyState = ({ icon: Icon, title, subtitle, children, className = '' }) => {
    return (
        <div className={`text-center py-12 text-gray-400 ${className}`}>
            {Icon && (
                <Icon className="mx-auto h-12 w-12 mb-4 opacity-40" />
            )}
            {title && (
                <p className="text-lg font-medium text-gray-500 dark:text-gray-400">
                    {title}
                </p>
            )}
            {subtitle && (
                <p className="text-sm mt-1 text-gray-400 dark:text-gray-500">
                    {subtitle}
                </p>
            )}
            {children && (
                <div className="mt-4">
                    {children}
                </div>
            )}
        </div>
    );
};

export default EmptyState;
