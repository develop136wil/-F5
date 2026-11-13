/** @type {import('tailwindcss').Config} */
export default {
    darkMode: 'class',
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                // Toss-style color palette inspiration
                primary: "#245FE0", // Contrast margin on white and the 10% selected background
                secondary: "#B1B8C0",
                background: "#F2F4F6", // Light Gray Background
                surface: "#FFFFFF",
                text: "#191F28", // Dark text
                "text-secondary": "#667085",
            },
            fontFamily: {
                // 설치형 앱은 오프라인에서도 같은 모양으로 읽혀야 한다. 외부 폰트 CDN 대신
                // 운영체제에 기본 포함된 한글 UI 글꼴을 순서대로 사용한다.
                sans: ['"Pretendard Variable"', 'Pretendard', '"Segoe UI"', '"Malgun Gothic"', '"Apple SD Gothic Neo"', 'sans-serif'],
            }
        },
    },
    plugins: [],
}

