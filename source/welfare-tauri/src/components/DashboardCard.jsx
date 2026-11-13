import React from 'react';
import { Card, CardContent, CardHeader, CardTitle } from './ui/Card';

const DashboardCard = ({ title, value, subtext, icon: Icon, onClick }) => {
    return (
        <Card
            onClick={onClick}
            onKeyDown={onClick ? (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onClick();
                }
            } : undefined}
            role={onClick ? 'button' : undefined}
            tabIndex={onClick ? 0 : undefined}
            aria-label={onClick ? `${title} 상세 화면 열기` : undefined}
            className={`hover:translate-y-[-2px] transition-transform duration-300 ${onClick ? 'cursor-pointer' : ''}`}
        >
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">
                    {title}
                </CardTitle>
                {Icon && <Icon className="h-4 w-4 text-gray-500" />}
            </CardHeader>
            <CardContent>
                <div className="text-2xl font-bold">{value}</div>
                <p className="text-xs text-gray-500">
                    {subtext}
                </p>
            </CardContent>
        </Card>
    );
};

export default DashboardCard;
