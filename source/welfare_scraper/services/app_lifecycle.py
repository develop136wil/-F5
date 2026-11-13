"""Desktop shutdown admission gate; no forced termination or process discovery."""
import hmac
import os

shutdown_requested = False
active_requests = 0
active_scheduled_jobs = 0
active_profile_jobs = 0


def token():
    return os.environ.get('WELFARE_DESKTOP_TOKEN', '')


class DesktopAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global active_requests
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/'):
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        # CORS only controls reading a response; reject foreign writes before
        # the application consumes a multipart upload or changes any state.
        headers = dict(scope.get('headers', []))
        if scope.get('method', 'GET').upper() not in ('GET', 'HEAD', 'OPTIONS'):
            origin = headers.get(b'origin')
            allowed = {b'tauri://localhost', b'http://tauri.localhost',
                       b'https://tauri.localhost', b'http://localhost:1420',
                       b'http://127.0.0.1:1420', b'http://localhost:8421',
                       b'http://127.0.0.1:8421'}
            if ((origin is not None and origin not in allowed) or
                    (origin is None and headers.get(b'sec-fetch-site') == b'cross-site')):
                return await JSONResponse({'detail': '외부 페이지의 변경 요청은 허용되지 않습니다.'}, status_code=403)(scope, receive, send)
        secret = token()
        supplied = dict(scope.get('headers', [])).get(b'x-welfare-session', b'')
        if secret and not hmac.compare_digest(secret.encode('utf-8'), supplied):
            return await JSONResponse({'detail': '이 실행의 연결 정보가 아닙니다.'}, status_code=403)(scope, receive, send)
        if scope['path'].startswith('/api/desktop/'):
            return await self.app(scope, receive, send)
        if shutdown_requested:
            return await JSONResponse({'detail': '프로그램을 안전하게 종료하고 있습니다.'}, status_code=503)(scope, receive, send)
        active_requests += 1
        try:
            # ASGI completion includes streaming responses and BackgroundTasks.
            await self.app(scope, receive, send)
        finally:
            active_requests -= 1
