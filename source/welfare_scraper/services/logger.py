import logging
import os
import sys
import threading
from collections import deque
from services.secret_redaction import SecretRedactionFilter, SecretRedactingFormatter

_log_lock = threading.Lock()
MEMORY_LOGS = deque(maxlen=50)

# (버그 2026-07-24) 패키징(frozen) 빌드에서 핸들러 오류가 나면 logging이 매번 전체
# 트레이스백+콜스택을 stderr로 덤프한다. 이 앱은 로그에 이모지를 많이 쓰는데 frozen
# 빌드의 sys.stdout이 cp949라 이모지 줄마다 UnicodeEncodeError가 났고, 그 덤프가
# 이벤트 루프 스레드에서 동기 파일 쓰기로 쏟아져 서버가 굶었다(실측: 트레이스백
# 7,484건 / stderr 로그 11MB → /api/status 응답 불가 → "서버 상태 확인 연속 실패").
# 근본 원인(콘솔 핸들러)은 아래에서 제거하고, 여기서는 어떤 핸들러 오류가 또 생기더라도
# 트레이스백 폭주로 번지지 않게 이중으로 막는다.
if getattr(sys, 'frozen', False):
    logging.raiseExceptions = False

class MemoryLogHandler(logging.Handler):
    """Stores last N logs in memory (thread-safe)."""
    def __init__(self, capacity=50):
        super().__init__()
        self.capacity = capacity

    def emit(self, record):
        try:
            msg = self.format(record)
            with _log_lock:
                MEMORY_LOGS.append(msg)
        except Exception:
            self.handleError(record)

def setup_logger(name=__name__, log_file='scraper.log'):
    """
    Sets up a logger with console, file, and memory handlers.
    """
    # Use AppData for logs to avoid permission issues
    app_data = os.getenv('LOCALAPPDATA')
    if not app_data:
        app_data = os.path.expanduser('~')
    
    base_dir = os.path.join(app_data, 'welfare-scraper')
    log_dir = os.path.join(base_dir, 'logs')

    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
        
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not any(isinstance(item, SecretRedactionFilter) for item in logger.filters):
        logger.addFilter(SecretRedactionFilter())
    
    # Check if handlers already exist to avoid duplicate logs
    if not logger.handlers:
        # File Handler (Rotating)
        try:
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                os.path.join(log_dir, log_file), 
                maxBytes=5*1024*1024, # 5MB
                backupCount=5,
                encoding='utf-8'
            )
            file_formatter = SecretRedactingFormatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            print(f"Failed to setup file handler: {e}")
        
        # Console Handler
        # frozen(패키징) 빌드는 애초에 콘솔이 없고, sys.stdout이 cp949라 이모지 로그마다
        # UnicodeEncodeError를 낸다 → 붙이지 않는다. 로그는 파일·메모리 핸들러로 충분하다.
        # 개발 실행에서는 콘솔 출력이 유용하므로, stdout을 UTF-8로 재설정해 이모지가
        # 깨지거나 예외를 내지 않게 한 뒤 붙인다.
        if sys.stdout and not getattr(sys, 'frozen', False):
            try:
                try:
                    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
                except Exception:
                    pass  # 재설정 불가한 스트림이면 아래 errors='replace' 없이도 진행
                console_handler = logging.StreamHandler(sys.stdout)
                console_formatter = SecretRedactingFormatter('%(asctime)s - %(levelname)s - %(message)s')
                console_handler.setFormatter(console_formatter)
                logger.addHandler(console_handler)
            except Exception as e:
                print(f"Failed to setup console handler: {e}")
        
        # Memory Handler (for UI)
        memory_handler = MemoryLogHandler()
        memory_formatter = SecretRedactingFormatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
        memory_handler.setFormatter(memory_formatter)
        logger.addHandler(memory_handler)
        
    # Handler filters also cover records propagated by child loggers.
    for handler in logger.handlers:
        if not any(isinstance(item, SecretRedactionFilter) for item in handler.filters):
            handler.addFilter(SecretRedactionFilter())
    return logger

def get_recent_logs():
    return list(MEMORY_LOGS)
