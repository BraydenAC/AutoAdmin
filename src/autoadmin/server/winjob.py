"""A Windows Job Object around the server JVM (the launcher's cgroup stand-in).

- Caps the JVM's committed memory (the systemd MemoryMax equivalent).
- Kills the JVM if the launcher dies, so no server runs that nothing controls.
- Accounts CPU time and peak memory.

Stdlib only (ctypes); the structures import on any OS so their layout can be tested.
"""

from __future__ import annotations

import ctypes
from ctypes import c_int64, c_size_t, c_uint32, c_uint64

JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", c_int64), ("PerJobUserTimeLimit", c_int64),
                ("LimitFlags", c_uint32), ("MinimumWorkingSetSize", c_size_t),
                ("MaximumWorkingSetSize", c_size_t), ("ActiveProcessLimit", c_uint32),
                ("Affinity", c_size_t), ("PriorityClass", c_uint32), ("SchedulingClass", c_uint32)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps",
                                        "ReadBytes", "WriteBytes", "OtherBytes")]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", c_size_t), ("JobMemoryLimit", c_size_t),
                ("PeakProcessMemoryUsed", c_size_t), ("PeakJobMemoryUsed", c_size_t)]


class BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = [("TotalUserTime", c_int64), ("TotalKernelTime", c_int64),
                ("ThisPeriodTotalUserTime", c_int64), ("ThisPeriodTotalKernelTime", c_int64),
                ("TotalPageFaultCount", c_uint32), ("TotalProcesses", c_uint32),
                ("ActiveProcesses", c_uint32), ("TotalTerminatedProcesses", c_uint32)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", c_uint32), ("PageFaultCount", c_uint32),
                *[(n, c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]


def _kernel32():
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD]
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _check(ok: int, what: str) -> None:
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{what} failed")


class Job:
    """Holds the job handle for the launcher's lifetime; closing it kills the JVM."""

    def __init__(self, memory_max_mb: int | None):
        self.k = _kernel32()
        self.handle = self.k.CreateJobObjectW(None, None)
        _check(self.handle, "CreateJobObject")
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_max_mb:
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = memory_max_mb * 1024 * 1024
        _check(self.k.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                              ctypes.byref(info), ctypes.sizeof(info)),
               "SetInformationJobObject")
        self.process = None

    def assign(self, pid: int) -> None:
        self.process = self.k.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
            | PROCESS_VM_READ, False, pid)
        _check(self.process, "OpenProcess")
        _check(self.k.AssignProcessToJobObject(self.handle, self.process),
               "AssignProcessToJobObject")

    def _query(self, cls: int, struct: ctypes.Structure) -> ctypes.Structure:
        _check(self.k.QueryInformationJobObject(self.handle, cls, ctypes.byref(struct),
                                                ctypes.sizeof(struct), None),
               "QueryInformationJobObject")
        return struct

    def stats(self) -> dict:
        """Bytes and nanoseconds, in the shape ProcessBackend.accounting returns."""
        limits = self._query(JobObjectExtendedLimitInformation, EXTENDED_LIMIT())
        acct = self._query(JobObjectBasicAccountingInformation, BASIC_ACCOUNTING())
        mem = PROCESS_MEMORY_COUNTERS(cb=ctypes.sizeof(PROCESS_MEMORY_COUNTERS))
        current = 0
        if self.process and self.k.K32GetProcessMemoryInfo(self.process, ctypes.byref(mem),
                                                           mem.cb):
            current = mem.PagefileUsage      # committed memory, what the cap limits
        return {
            "memory": current,
            "memory_peak": limits.PeakJobMemoryUsed,
            "memory_max": limits.JobMemoryLimit,
            "cpu_ns": (acct.TotalUserTime + acct.TotalKernelTime) * 100,
        }

    def hit_memory_limit(self) -> bool:
        """Whether the JVM got within 2% of the cap (its allocations then fail)."""
        limits = self._query(JobObjectExtendedLimitInformation, EXTENDED_LIMIT())
        return bool(limits.JobMemoryLimit) and \
            limits.PeakJobMemoryUsed >= limits.JobMemoryLimit * 0.98
