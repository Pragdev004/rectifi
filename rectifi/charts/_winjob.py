"""Windows limits for the chart child, through a Job Object (ctypes, kernel32). Written for Windows; NOT run in Linux CI.

    limit_self(memory_bytes, cpu_seconds)  called by _child.py on itself: a new job with a process memory cap, a CPU-time cap,
                                           a limit of one active process (no child processes) and die-on-unhandled-exception.
    ParentJob                              held by the parent: kills the child (and, when the venv launcher started it, the real
                                           interpreter) on timeout.

A venv's python.exe can be a small launcher that starts the real interpreter as a child. So the child limits itself (no race
with the launcher) and the parent assigns the launcher it started to a second job whose only purpose is to kill the whole tree.
If any call fails, the caller falls back to the wall-clock timeout alone and records limits "timeout-only".
Importing this file on Linux is harmless (nothing touches windll at import).
"""

from __future__ import annotations

import ctypes
import sys

JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


def _structures():
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return EXTENDED


def _kernel32():
    k = ctypes.windll.kernel32            # type: ignore[attr-defined]  (Windows only)
    k.CreateJobObjectW.restype = ctypes.c_void_p
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def _new_job(k, flags: int, *, memory_bytes: int = 0, cpu_seconds: int = 0, active: int = 0):
    EXTENDED = _structures()
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObject failed")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = flags
    if flags & JOB_OBJECT_LIMIT_PROCESS_TIME:
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(cpu_seconds) * 10_000_000      # 100 ns units
    if flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS:
        info.BasicLimitInformation.ActiveProcessLimit = active
    if flags & JOB_OBJECT_LIMIT_PROCESS_MEMORY:
        info.ProcessMemoryLimit = int(memory_bytes)
    if not k.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError("SetInformationJobObject failed")
    return job


def limit_self(memory_bytes: int, cpu_seconds: int) -> None:
    """Put the calling process in a job with the limits above. Raises OSError if Windows refuses."""
    if sys.platform != "win32":
        raise OSError("not Windows")
    k = _kernel32()
    flags = (JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
             | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)
    job = _new_job(k, flags, memory_bytes=memory_bytes, cpu_seconds=cpu_seconds, active=1)
    if not k.AssignProcessToJobObject(job, k.GetCurrentProcess()):
        raise OSError("AssignProcessToJobObject failed")


class ParentJob:
    """A job whose handle the parent keeps, so closing it (or terminate()) kills everything assigned to it."""

    def __init__(self) -> None:
        self.handle = None
        self.k = None
        if sys.platform != "win32":
            return
        try:
            self.k = _kernel32()
            self.handle = _new_job(self.k, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        except OSError:
            self.handle = None

    def assign(self, pid: int) -> bool:
        if not self.handle:
            return False
        process = self.k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            return False
        try:
            return bool(self.k.AssignProcessToJobObject(self.handle, process))
        finally:
            self.k.CloseHandle(process)

    def terminate(self) -> None:
        if self.handle:
            self.k.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None
