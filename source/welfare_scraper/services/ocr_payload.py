"""Short-lived, bounded PDF bytes for the local-extraction -> OCR handoff.

Never persisted or serialized. Falling back to the existing URL downloader is
intentional when the two-buffer/40 MiB process-wide budget is already occupied.
"""
import threading


class OcrPayload:
    MAX_BYTES = 40 * 1024 * 1024
    MAX_ITEMS = 2
    _lock = threading.Lock()
    _bytes = 0
    _items = 0

    def __init__(self):
        self.data = None
        self.url = ''

    @classmethod
    def retain(cls, url, data):
        if not isinstance(data, bytes) or not data:
            return None
        with cls._lock:
            if cls._items >= cls.MAX_ITEMS or cls._bytes + len(data) > cls.MAX_BYTES:
                return None
            payload = cls()
            payload.url, payload.data = url, data
            cls._items += 1
            cls._bytes += len(data)
            return payload

    def close(self):
        cls = type(self)
        with cls._lock:
            if self.data is not None:
                cls._bytes -= len(self.data)
                cls._items -= 1
                self.data = None

    def __del__(self):
        # Safety net only; collectors explicitly close on completion/cancellation.
        self.close()
