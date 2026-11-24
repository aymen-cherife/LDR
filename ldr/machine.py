"""``ldr check_machine``: is this machine fit to run and to time a recording?

The facts on one line, the processes using the CPU, then one line per problem and per fix. It runs on its own and with every ``ldr run``.
With a run nothing waits for it: the CPU sample runs beside the program's start and the result is
printed when it is ready, and the Python start time is the run's own (launch to first recorded
call). It never blocks a run and never changes an exit code.

It fixes what is LDR's own and says so: runs whose supervisor died are marked finished and their
rows removed; runs beyond ``keep_runs`` are deleted (at the end of a run, with a run). What is not
LDR's (another process, another file, a live run) is never touched: the line gives the number and
the exact command to run, for Windows and for Linux.

No dependency: Windows system calls through ctypes, /proc on Linux.

It reports facts. There is no threshold in it: no CPU share is "busy", no start time is "slow", no
age makes a run "old". A problem is something that is objectively wrong: a run whose process is
gone, a locked database, stored runs beyond keep_runs, less free disk than the database is big.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from . import processes

SAMPLE_SECONDS = 1.0
WINDOWS = os.name == "nt"


@dataclass
class Problem:
    kind: str
    text: str
    number: int | None = None
    windows: str | None = None
    linux: str | None = None

    def line(self) -> str:
        extra = ""
        if self.windows or self.linux:
            extra = f" - stop: Windows `{self.windows}`  Linux `{self.linux}`"
        return f"- {self.text}{extra}"

    def data(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass
class Report:
    problems: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    context: str = "alone"
    ldr_dir: str = ""
    sample: dict | None = None  # raw CPU sample: {"percent", "processes": {pid: percent}}
    processes: list = field(default_factory=list)  # filled by finalize()

    def finalize(self, first_call_s: float | None = None) -> "Report":
        """Name the processes of the CPU sample (LDR's own, or not) and build the facts line.

        Done when the report is shown, not when the sample is taken: by then the run's supervisor has
        written its process number, so that LDR's own processes are recognised."""
        if first_call_s is not None:
            self.facts["first_call_s"] = first_call_s
        self.processes = []
        if self.sample:
            ours = _ldr_pids(scan_runs(self.ldr_dir)) if self.ldr_dir else {os.getpid()}
            names = {pid: name for pid, _, name in list_processes()}
            for pid, percent in sorted(self.sample["processes"].items(), key=lambda item: -item[1]):
                identity = process_identity(pid)
                self.processes.append({
                    "pid": pid, "name": names.get(pid, "?"), "percent_of_one_core": percent, "ldr": pid in ours,
                    "started": time.strftime("%d %b %H:%M", time.localtime(identity[0])) if identity else "?",
                })
        self.facts["processes"] = self.processes
        return self

    def summary(self) -> str:
        facts = self.facts
        head = "machine: " + (f"{len(self.problems)} problem(s)" if self.problems else "ok")
        parts = [f"{facts.get('runs_alive', 0)} run(s) alive"]
        if "disk_free_gb" in facts:
            parts.append(f"disk {facts['disk_free_gb']} GB free ({facts['disk_free_percent']}%)")
        if "database_mb" in facts:
            parts.append(f"database {facts['database_mb']} MB")
        parts.append(f"runs stored {facts.get('runs_stored', 0)} / keep_runs {facts.get('keep_runs', '?')}")
        if "cpu_percent" in facts:
            parts.append(f"CPU {facts['cpu_percent']:.0f}% busy")
        if "python_start_s" in facts and self.context == "alone":
            parts.append(f"python start {facts['python_start_s']} s")
        if self.context == "run":
            parts.append(f"first call {facts['first_call_s']} s" if "first_call_s" in facts else "first call: none")
        python = facts["python"]
        parts.append(f"Python {python['version']}")
        parts.append("venv " + ("yes" if python["venv"] else "no"))
        parts.append(f"check {self.cost.get('wall_s', 0)} s wall, {self.cost.get('cpu_s', 0)} s CPU")
        return head + " - " + ", ".join(parts)

    def process_lines(self) -> list[str]:
        shown = self.processes if self.context == "alone" else self.processes[:5]
        lines = []
        for item in shown:
            label = f"  {item['name']} (process {item['pid']}, started {item['started']}) {item['percent_of_one_core']:.0f}% of one core"
            if item["ldr"]:
                lines.append(label + " - LDR")
            else:
                lines.append(label + f" - stop: Windows `taskkill /PID {item['pid']} /F`  Linux `kill {item['pid']}`")
        if self.context == "run" and len(self.processes) > len(shown):
            lines.append(f"  and {len(self.processes) - len(shown)} more: ldr check_machine lists them all")
        return lines

    def lines(self) -> list[str]:
        return [self.summary(), *self.process_lines(), *(p.line() for p in self.problems), *(f"fixed: {fix}" for fix in self.fixes)]

    def data(self) -> dict:
        return {
            "summary": self.summary(), "problems": [p.data() for p in self.problems], "fixes": list(self.fixes),
            "facts": self.facts, "cost": self.cost,
        }


# ---------------------------------------------------------------- processes

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3

    class _Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]

    def _seconds(ft) -> float:
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    def _open(pid: int):
        handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        return handle or None


def process_identity(pid: int):
    """(start time as epoch seconds, executable name in lower case) of a live process, else None."""
    if not pid:
        return None
    if WINDOWS:
        handle = _open(pid)
        if not handle:
            return None
        try:
            created, left, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _k32.GetProcessTimes(handle, *map(ctypes.byref, (created, left, kernel, user))):
                return None
            if _seconds(left) > 0:  # exited
                return None
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            name = ""
            if _k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                name = os.path.basename(buffer.value).lower()
            return _seconds(created) - 11644473600, name
        finally:
            _k32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as handle:
            name = handle.read().strip().lower()
        with open("/proc/stat", "rb") as handle:
            boot = next(int(line.split()[1]) for line in handle if line.startswith(b"btime"))
        return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK"), name
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def list_processes() -> list[tuple[int, int, str]]:
    """[(pid, parent pid, name)] of every process this user can see."""
    found = []
    if WINDOWS:
        snapshot = _k32.CreateToolhelp32Snapshot(2, 0)
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
        _k32.CloseHandle(snapshot)
        return found
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    text = handle.read()
                comm = text[text.index(b"(") + 1:text.rindex(b")")].decode("utf-8", "replace").lower()
                found.append((int(name), int(text.rsplit(b")", 1)[1].split()[1]), comm))
            except (OSError, ValueError, IndexError):
                pass
    return found


def _cpu_seconds(pid: int):
    if WINDOWS:
        handle = _open(pid)
        if not handle:
            return None
        try:
            created, left, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if _k32.GetProcessTimes(handle, *map(ctypes.byref, (created, left, kernel, user))):
                return _seconds(kernel) + _seconds(user)
            return None
        finally:
            _k32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _system_times():
    """(busy seconds, total seconds) of all the CPUs since boot."""
    if WINDOWS:
        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        _k32.GetSystemTimes(*map(ctypes.byref, (idle, kernel, user)))
        total = _seconds(kernel) + _seconds(user)  # the kernel time includes the idle time
        return total - _seconds(idle), total
    with open("/proc/stat", "rb") as handle:
        values = [int(v) for v in handle.readline().split()[1:]]
    total = sum(values[:8]) / os.sysconf("SC_CLK_TCK")
    return total - (values[3] + values[4]) / os.sysconf("SC_CLK_TCK"), total


def sample_cpu(seconds: float = SAMPLE_SECONDS) -> dict:
    """{"percent": all the CPU, "processes": {pid: percent of one core}} over ``seconds``."""
    pids = [pid for pid, _, _ in list_processes()]
    before = {pid: _cpu_seconds(pid) for pid in pids}
    system_before = _system_times()
    started = time.perf_counter()
    time.sleep(seconds)
    elapsed = time.perf_counter() - started
    system_after = _system_times()
    used = {}
    for pid, first in before.items():
        if first is None:
            continue
        second = _cpu_seconds(pid)
        if second is not None and second > first:
            used[pid] = round((second - first) / elapsed * 100, 1)
    total = system_after[1] - system_before[1]
    percent = round((system_after[0] - system_before[0]) / total * 100, 1) if total > 0 else 0.0
    return {"percent": percent, "processes": used, "cpus": os.cpu_count() or 1}


def holders(path: str) -> list[tuple[int, str]]:
    """The processes that have this file open: [(pid, name)]. Windows: the Restart Manager."""
    found: list[tuple[int, str]] = []
    if WINDOWS:
        try:
            found = _restart_manager(path)
        except Exception:
            found = []
    else:
        target = os.path.realpath(path)
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                for descriptor in os.listdir(f"/proc/{name}/fd"):
                    if os.path.realpath(f"/proc/{name}/fd/{descriptor}") == target:
                        identity = process_identity(int(name))
                        found.append((int(name), identity[1] if identity else ""))
                        break
            except OSError:
                pass
    return [(pid, name) for pid, name in found if pid != os.getpid()]


def _restart_manager(path: str):
    class Unique(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    class Info(ctypes.Structure):
        _fields_ = [
            ("Process", Unique), ("strAppName", ctypes.c_wchar * 256), ("strServiceShortName", ctypes.c_wchar * 64),
            ("ApplicationType", wintypes.DWORD), ("AppStatus", ctypes.c_ulong), ("TSSessionId", wintypes.DWORD),
            ("bRestartable", wintypes.BOOL),
        ]

    rm = ctypes.WinDLL("rstrtmgr")
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(session), 0, key):
        return []
    try:
        files = (wintypes.LPCWSTR * 1)(path)
        if rm.RmRegisterResources(session, 1, files, 0, None, 0, None):
            return []
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD()
        code = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if code == 0 and needed.value == 0:
            return []
        infos = (Info * max(needed.value, 1))()
        count = wintypes.UINT(len(infos))
        if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons)):
            return []
        return [(infos[i].Process.dwProcessId, infos[i].strAppName) for i in range(count.value)]
    finally:
        rm.RmEndSession(session)


# ---------------------------------------------------------------- LDR's own runs

def supervisor_alive(state: dict) -> bool:
    """Whether the supervisor named in a run's state is that process, not a reused number."""
    identity = process_identity(state.get("pid") or 0)
    if identity is None or "python" not in identity[1]:
        return False
    start = state.get("pid_start")
    if start is not None:
        return abs(identity[0] - start) < 2.0
    started = state.get("started_at")
    return started is None or identity[0] >= started - 5


def scan_runs(ldr_dir: str) -> dict:
    """The runs in .ldr/runs: {"live": [...], "dead": [...], "unstarted": [...]} (run directories)."""
    result = {"live": [], "dead": [], "unstarted": [], "owned": {}, "dead_ids": set()}
    base = os.path.join(ldr_dir, "runs")
    try:
        names = os.listdir(base)
    except OSError:
        return result
    for name in names:
        directory = os.path.join(base, name)
        try:
            with open(os.path.join(directory, "state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            result["unstarted"].append(directory)  # no state yet: a run that is starting (or a launch that failed)
            continue
        if state.get("store_run_id"):
            result["owned"][state["store_run_id"]] = name
        if state.get("state") == "finished":
            if state.get("dead") and state.get("store_run_id"):
                result["dead_ids"].add(state["store_run_id"])
            continue
        (result["live"] if supervisor_alive(state) else result["dead"]).append(directory)
    return result


def mark_dead(directory: str) -> str | None:
    """Mark a dead run finished (its own state file); returns the store run id it owned."""
    path = os.path.join(directory, "state.json")
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    state.update(state="finished", dead=True, exit_code=2, ended_at=time.time(),
                 message="LDR marked this run dead: its supervisor process was gone without a result (ldr check_machine)")
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(temporary, path)
    return state.get("store_run_id")


def delete_rows(connection, run_ids: list[str]) -> None:
    from .previous import _TABLES

    with connection:
        for chunk in (run_ids[i:i + 100] for i in range(0, len(run_ids), 100)):
            marks = ",".join("?" * len(chunk))
            for table in _TABLES:
                connection.execute(f"DELETE FROM {table} WHERE run_id IN ({marks})", chunk)
            connection.execute(f"DELETE FROM runs WHERE id IN ({marks})", chunk)


def unfinished_rows(connection) -> list[tuple[str, int]]:
    return connection.execute("SELECT id, started_ns FROM runs WHERE status IS NULL").fetchall()


def clean_dead(ldr_dir: str, scan: dict, report: Report, *, rows: bool) -> None:
    """Mark dead runs finished and (when ``rows``) remove the rows of runs that no process can own.

    A row of an unfinished run is deleted when the run's own state says its supervisor is dead, or
    when it belongs to no run directory and nothing is alive or starting that could have made it."""
    dead_ids = set(scan["dead_ids"])
    for directory in scan["dead"]:
        try:
            owned = mark_dead(directory)
        except (OSError, ValueError):
            continue
        report.fixes.append(f"marked dead run {os.path.basename(directory)} finished (its supervisor was gone)")
        if owned:
            dead_ids.add(owned)
    database = os.path.join(ldr_dir, "traces.sqlite3")
    if not rows or not os.path.exists(database):
        return
    nobody_can_own_an_orphan = not scan["live"] and not scan["unstarted"]
    live_ids = set()
    for directory in scan["live"]:
        try:
            with open(os.path.join(directory, "state.json"), encoding="utf-8") as handle:
                live_ids.add(json.load(handle).get("store_run_id"))
        except (OSError, ValueError):
            pass
    try:
        with closing(sqlite3.connect(database, timeout=2)) as connection:
            stale = []
            for run_id, _started in unfinished_rows(connection):
                if run_id in live_ids:
                    continue
                if run_id in dead_ids or (run_id not in scan["owned"] and nobody_can_own_an_orphan):
                    stale.append(run_id)
            if stale:
                delete_rows(connection, stale)
                report.fixes.append(f"deleted the rows of {len(stale)} run(s) that never finished")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- the checks

def _antivirus(ldr_dir: str, report: Report) -> None:
    """Windows: what can be read, without admin, about real-time scanning and .ldr (a fact, not a problem)."""
    if not WINDOWS:
        return
    import winreg

    def read(subkey, name=None):
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
            if name is not None:
                return winreg.QueryValueEx(key, name)[0]
            return [winreg.EnumValue(key, i)[0] for i in range(winreg.QueryInfoKey(key)[1])]

    base = r"SOFTWARE\Microsoft\Windows Defender"
    try:
        disabled = read(base + r"\Real-Time Protection", "DisableRealtimeMonitoring")
    except OSError:
        disabled = None
    try:
        exclusions = read(base + r"\Exclusions\Paths")
    except OSError:
        exclusions = None
    path = os.path.normcase(os.path.abspath(ldr_dir))
    report.facts["antivirus"] = {
        "real_time_scanning": None if disabled is None else not disabled,
        "exclusions_readable": exclusions is not None,
        "ldr_excluded": None if exclusions is None else any(
            path.startswith(os.path.normcase(os.path.abspath(item))) for item in exclusions
        ),
    }


def _python_start(report: Report) -> None:
    started = time.perf_counter()
    processes.run([sys.executable, "-c", "pass"], capture_output=True, check=False, timeout=300)
    report.facts["python_start_s"] = round(time.perf_counter() - started, 2)


def check(ldr_dir: str, *, keep: int, context: str = "alone", fix: bool = True, sample: bool = True, python_start: bool = True) -> Report:
    """Run every check. ``context`` is "alone" or "run"; with a run, fixes that touch the database
    wait for the end of the run, and the Python start time is the run's own (the first call)."""
    started, cpu_started = time.perf_counter(), time.process_time()
    ldr_dir = os.path.abspath(ldr_dir)
    report = Report(context=context, ldr_dir=ldr_dir)
    facts = report.facts
    # disk
    probe = ldr_dir if os.path.isdir(ldr_dir) else os.path.dirname(ldr_dir)
    usage = shutil.disk_usage(probe)
    facts["disk_free_gb"] = round(usage.free / 2**30, 1)
    facts["disk_free_percent"] = round(usage.free / usage.total * 100, 1)
    # LDR's runs
    scan = scan_runs(ldr_dir)
    facts["runs_alive"], facts["runs_dead"] = len(scan["live"]), len(scan["dead"])
    for directory in scan["dead"]:
        report.problems.append(Problem("dead_run", f"run {os.path.basename(directory)} died without a result"))
    # the database
    database = os.path.join(ldr_dir, "traces.sqlite3")
    stored = beyond = 0
    size = 0
    if os.path.exists(database):
        size = os.path.getsize(database) + (os.path.getsize(database + "-wal") if os.path.exists(database + "-wal") else 0)
        facts["database_mb"] = round(size / 2**20)
        locked = _database_locked(database, exclusive=not scan["live"])
        if locked:
            who = holders(database)
            ours = _ldr_pids(scan)
            names = ", ".join(f"{name or '?'} (process {pid})" + (" - LDR" if pid in ours else "") for pid, name in who) \
                or "a process that could not be named"
            others = [(pid, name) for pid, name in who if pid not in ours]
            number = others[0][0] if others else None
            report.problems.append(Problem(
                "database_locked", f"the database is locked, held by {names}", number=number,
                windows=f"taskkill /PID {number} /F" if number else None, linux=f"kill {number}" if number else None,
            ))
        facts["database_locked"] = bool(locked)
        try:
            from . import previous

            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)) as connection:
                stored = connection.execute("SELECT COUNT(*) FROM runs WHERE status IS NOT NULL").fetchone()[0]
                beyond = len(previous.doomed(connection, keep))
        except (sqlite3.Error, OSError):
            pass
    facts["runs_stored"], facts["keep_runs"], facts["runs_beyond_keep"] = stored, keep, beyond
    if beyond:
        report.problems.append(Problem(
            "keep_runs", f"{beyond} stored run(s) are beyond keep_runs = {keep}"
            + (" (deleted at the end of this run)" if context == "run" else ""),
        ))
    if size and usage.free < size:
        report.problems.append(Problem(
            "disk", f"{usage.free / 2**30:.1f} GB free is less than the database ({size / 2**30:.1f} GB): the next run may not fit",
        ))
    # CPU: the processes using it, from the sample (no judgement)
    if sample:
        report.sample = sample_cpu()
        facts["cpu_percent"] = report.sample["percent"]
    # Python
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    facts["python"] = {"version": sys.version.split()[0], "venv": sys.prefix if in_venv else None, "executable": sys.executable}
    if context == "alone" and python_start:
        _python_start(report)
    _antivirus(ldr_dir, report)
    # fixes (LDR's own leftovers only)
    if fix:
        clean_dead(ldr_dir, scan, report, rows=context == "alone")
        if context == "alone" and beyond:
            from . import previous

            deleted = previous.prune(ldr_dir, keep)
            if deleted:
                report.fixes.append(f"deleted {len(deleted)} stored run(s) beyond keep_runs = {keep}")
        fixed_kinds = set()
        if any(fix.startswith("marked dead run") for fix in report.fixes):
            fixed_kinds.add("dead_run")
        if context == "alone" and beyond and any("beyond keep_runs" in fix for fix in report.fixes):
            fixed_kinds.add("keep_runs")
        report.problems = [p for p in report.problems if p.kind not in fixed_kinds]
    report.cost = {"wall_s": round(time.perf_counter() - started, 3), "cpu_s": round(time.process_time() - cpu_started, 3)}
    return report.finalize()


def _database_locked(database: str, *, exclusive: bool) -> bool:
    """Whether the database cannot be used now. With no live run it asks for the write lock for an
    instant; with one it only reads, so that a live run never waits for this."""
    try:
        if exclusive:
            with closing(sqlite3.connect(database, timeout=0)) as connection:
                connection.isolation_level = None
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("ROLLBACK")
        else:
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0)) as connection:
                connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc) or "busy" in str(exc)
    return False


def _ldr_pids(scan: dict) -> set[int]:
    """The processes of this machine's live LDR runs and their descendants, and this process (not its children)."""
    roots = set()
    for directory in scan["live"]:
        try:
            with open(os.path.join(directory, "state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
            roots.update(p for p in (state.get("pid"), state.get("program_pid")) if p)
        except (OSError, ValueError):
            pass
    family = set(roots)
    table = list_processes()
    changed = True
    while changed:
        changed = False
        for pid, parent, _ in table:
            if parent in family and pid not in family:
                family.add(pid)
                changed = True
    family.add(os.getpid())
    return family


# ---------------------------------------------------------------- with a run

class Background:
    """The check of a run: starts at once, never waits, and is shown when the CPU sample is ready and
    the run's first call is known (or at the end of the run)."""

    def __init__(self, ldr_dir: str, keep: int):
        self.ldr_dir, self.keep = ldr_dir, keep
        self.report: Report | None = None
        self.printed = False
        self.thread = threading.Thread(target=self._work, name="ldr-machine", daemon=True)
        self.thread.start()

    def _work(self) -> None:
        try:
            self.report = check(self.ldr_dir, keep=self.keep, context="run")
        except Exception as exc:  # a check must never disturb a run
            failed = Report(context="run")
            failed.facts["python"] = {"version": sys.version.split()[0], "venv": None}
            failed.problems.append(Problem("check_failed", f"the check failed ({type(exc).__name__}: {exc})"))
            self.report = failed

    def ready(self) -> bool:
        return self.report is not None

    def print_once(self, state: dict | None = None, stream=None) -> None:
        if self.report is None or self.printed:
            return
        self.printed = True
        self.report.finalize((state or {}).get("first_call_s")) if self.report.sample else None
        if "first_call_s" in (state or {}):
            self.report.facts["first_call_s"] = state["first_call_s"]
        lines = self.report.lines()
        print("\n".join(f"[ldr] {line}" if i == 0 else line for i, line in enumerate(lines)), file=stream or sys.stderr, flush=True)

    def finish(self, state: dict) -> Report | None:
        """After the run: the report with the run's first call (launch to first recorded call)."""
        if self.report is None:
            self.thread.join(30)
        report = self.report
        if report is None:
            return None
        if state.get("first_call_s") is not None:
            report.facts["first_call_s"] = state["first_call_s"]
            report.facts["first_call_measured"] = "launch of the run to its first recorded call"
        return report


def save(ldr_dir: str, report: Report, *, merge_after: float | None = None) -> None:
    """Write .ldr/machine.json; with ``merge_after``, also put it in last.json (status.machine) when
    that file was written by the run just finished."""
    try:
        with open(os.path.join(ldr_dir, "machine.json"), "w", encoding="utf-8") as handle:
            json.dump(report.data(), handle, indent=1)
        if merge_after is not None:
            path = os.path.join(ldr_dir, "last.json")
            if os.path.getmtime(path) >= merge_after:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data.get("status"), dict):
                    data["status"]["machine"] = report.data()
                    temporary = f"{path}.{os.getpid()}.tmp"
                    with open(temporary, "w", encoding="utf-8") as handle:
                        json.dump(data, handle, indent=1)
                    os.replace(temporary, path)
    except (OSError, ValueError):
        pass


def main(argv: list[str], ldr_dir: str) -> int:
    """``ldr check_machine [--no-fix] [--json]``. Always exits 0."""
    from .previous import keep_runs

    report = check(ldr_dir, keep=keep_runs(os.path.dirname(ldr_dir)), context="alone", fix="--no-fix" not in argv)
    if "--json" in argv:
        print(json.dumps(report.data(), indent=1))
    else:
        print("\n".join(report.lines()))
    if os.path.isdir(ldr_dir):
        save(ldr_dir, report)
    return 0
