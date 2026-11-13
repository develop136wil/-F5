"""Hold a handle to the exact desktop process, so PID reuse cannot fool shutdown."""
import os


class ParentWatch:
    def __init__(self, pid):
        self.handle = None
        self.kernel = None
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only

    def alive(self):
        if os.name != 'nt':
            return True  # Windows EXE feature; do not guess ownership elsewhere.
        if not self.handle:
            return False
        return self.kernel.WaitForSingleObject(self.handle, 0) != 0

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
