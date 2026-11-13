import React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null, errorInfo: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        this.setState({ errorInfo });
        console.error('ErrorBoundary caught:', error, errorInfo);
    }

    handleReset = () => {
        this.setState({ hasError: false, error: null, errorInfo: null });
    };

    render() {
        if (this.state.hasError) {
            return (
                <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-6">
                    <div className="max-w-md w-full text-center space-y-6">
                        <div className="mx-auto w-16 h-16 bg-red-100 dark:bg-red-900/30 rounded-full flex items-center justify-center">
                            <AlertTriangle className="w-8 h-8 text-red-500" />
                        </div>
                        <div>
                            <h2 className="text-xl font-bold text-gray-800 dark:text-gray-100 mb-2">
                                문제가 발생했습니다
                            </h2>
                            <p className="text-sm text-gray-500 dark:text-gray-400">
                                예기치 않은 오류가 발생했습니다. 아래 버튼을 눌러 다시 시도해 주세요.
                            </p>
                        </div>
                        {this.state.error && (
                            <details className="text-left bg-gray-100 dark:bg-gray-800 rounded-lg p-4">
                                <summary className="text-xs text-gray-500 cursor-pointer font-medium">
                                    오류 상세 정보
                                </summary>
                                <pre className="mt-2 text-xs text-red-600 dark:text-red-400 overflow-auto max-h-40 whitespace-pre-wrap">
                                    {this.state.error.toString()}
                                </pre>
                            </details>
                        )}
                        <button
                            onClick={this.handleReset}
                            className="inline-flex items-center gap-2 px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors font-medium"
                        >
                            <RefreshCw className="w-4 h-4" />
                            다시 시도
                        </button>
                    </div>
                </div>
            );
        }

        return this.props.children;
    }
}

export default ErrorBoundary;
