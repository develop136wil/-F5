"""Nonblocking OS lease for the whole profile workflow, not the GUI lifetime.

Recovery, launch and DB restore share this lease. Acquire it before any
maintenance/AI/scrape locks; restore holds it through cancellation and rollback.
Existence/PIDs never prove ownership:
access failures and another live holder both fail closed. The OS releases the
lease after abnormal process termination; the lock file itself is never removed.
"""
import os
from contextlib import contextmanager


def _lock_path():
    import database
    return os.path.join(os.path.dirname(os.path.abspath(database.DB_FILE)), 'news_workflow.lock')


class WorkflowLease:
    def __init__(self, handle):
        self._handle = handle
        self.transferred = False
        self._pins = 0
        self._closing = False

    @contextmanager
    def pin(self):
        """Keep cancellation acknowledgement inside the task's same lease."""
        if self._handle is None:
            raise RuntimeError('Workflow ownership already released')
        self._pins += 1
        try:
            yield
        finally:
            self._pins -= 1
            if self._closing:
                self.close()

    def close(self):
        self._closing = True
        if self._pins:
            return
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def acquire():
    """Return a lease, or None when busy/unavailable. Never remove a stale file."""
    handle = None
    try:
        handle = open(_lock_path(), 'a+b')
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return WorkflowLease(handle)
    except OSError:
        if handle is not None:
            handle.close()
        return None
