"""Own process trees for the explicitly enabled trusted-host adapter."""

import os
import signal
import subprocess


class ProcessTree:
    def __init__(self, argv, cwd, env):
        self.job = None
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                   "cwd": cwd, "env": env, "shell": False}
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel = kernel
            self.job = kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise OSError("job object unavailable")
            class BasicLimits(ctypes.Structure):
                _fields_ = [("ProcessTime", ctypes.c_longlong), ("JobTime", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                            ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("Basic", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                            ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                            ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

            limits = ExtendedLimits()
            limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            if not kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                kernel.CloseHandle(self.job)
                raise OSError("could not set process tree limits")
            # Create suspended, bind the tree before project code can spawn children, then resume.
            self.process = subprocess.Popen(argv, creationflags=0x4 | subprocess.CREATE_NO_WINDOW, **options)
            if not kernel.AssignProcessToJobObject(self.job, int(self.process._handle)):
                self.process.kill()
                kernel.CloseHandle(self.job)
                raise OSError("could not bind process tree")
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            if ntdll.NtResumeProcess(int(self.process._handle)) != 0:
                self.close()
                raise OSError("could not resume process")
        else:
            self.process = subprocess.Popen(argv, start_new_session=True, **options)

    def close(self):
        if os.name == "nt" and self.job:
            self.kernel.TerminateJobObject(self.job, 1)
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.wait(timeout=10)
