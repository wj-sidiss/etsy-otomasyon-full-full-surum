"""The single entry point the downloadable app is built from.

One executable does both jobs. Double-clicked, with no arguments, it starts the
local web app and opens it in the browser (`etsyotomasyon.web.launch`). Given arguments
— `etsyotomasyon.exe pinterest post` from Task Scheduler, say — it is the command line
tool, so the download never needs a Python install for either. `--self-test`
checks a packaged build (`etsyotomasyon.web.self_test`).
"""

from __future__ import annotations

import io
import os
import sys
import traceback
from datetime import date
from typing import Callable


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    # macOS adds a process serial number when an app is opened from Finder.
    args = [a for a in args if not a.startswith("-psn_")]
    if args == ["--self-test"]:
        _ensure_output()
        sys.exit(_guarded(self_test))
    if args:
        _ensure_output()
        sys.exit(_guarded(lambda: _run_as_cli(args)))
    try:
        from ..web import launch

        code = launch()
    except Exception:  # noqa: BLE001 — the app never started; say why, somewhere visible
        _report_crash(traceback.format_exc())
        sys.exit(1)
    sys.exit(code)


def _guarded(run: Callable[[], int]) -> int:
    """Run, and turn anything that escapes into a traceback and exit code 1.

    Nothing may reach a frozen app's bootloader: PyInstaller answers an uncaught
    exception with a modal message box, so a scheduled run or the release check
    would wait for someone to click OK — for hours.
    """
    try:
        return run()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        return 130
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
        return 1


def _ensure_output() -> None:
    """Send what a command prints somewhere a person can read it.

    From a terminal, that is the terminal. Double-clicked or run by Task Scheduler,
    the app's console is hidden or absent, so output goes to a dated log file in
    ~/.etsyotomasyon/logs instead, and a prompt reads end-of-file rather than hanging.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    if _attach_console():
        return
    _log_to_file()


def _report_crash(text: str) -> None:
    """The app could not start: log it, and on Windows say so in a message box."""
    try:
        _log_to_file()
        print(text, file=sys.stderr, flush=True)
    except OSError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0, "etsyotomasyon could not start:\n\n" + text[-1500:], "etsyotomasyon", 0x10
            )
        except (AttributeError, OSError):
            pass


def _run_as_cli(args: list[str]) -> int:
    """Behave exactly like the `etsyotomasyon` command."""
    from ..cli import main as cli_main

    sys.argv = ["etsyotomasyon", *args]
    try:
        cli_main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


def _attach_console() -> bool:
    """Borrow the console of the terminal that started this windowed app.

    In the one-file build Python runs in a child of PyInstaller's bootloader, and
    the bootloader — windowed too — has no console, so "attach to parent" alone
    finds nothing. The terminal is the bootloader's parent, one step further up.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        attach = ctypes.windll.kernel32.AttachConsole
        candidates = [-1]  # ATTACH_PARENT_PROCESS
        if getattr(sys, "frozen", False):
            grandparent = _parent_of(os.getppid())
            if grandparent:
                candidates.append(grandparent)
        if not any(attach(pid) for pid in candidates):
            return False
        out = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdout = sys.stderr = out
        sys.stdin = open("CONIN$", encoding="utf-8", errors="replace")  # noqa: SIM115
        return True
    except OSError:
        return False


def _parent_of(pid: int) -> int | None:
    """The parent process id of `pid`, from a Toolhelp snapshot (Windows only)."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


def _log_to_file() -> None:
    from ..config import home_dir

    folder = home_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / f"etsyotomasyon-{date.today().isoformat()}.log", "a", encoding="utf-8")  # noqa: SIM115
    sys.stdout = sys.stderr = handle
    # Nobody is there to answer a prompt; end-of-file makes it cancel instead of hang.
    sys.stdin = io.StringIO("")


def self_test() -> int:
    """Prove a built executable is complete. Run by the release workflow on the
    packaged app, where a missing module or data file shows up — not in a source
    checkout, where it cannot. See `etsyotomasyon.web.self_test`."""
    from ..web import self_test as web_self_test

    return web_self_test()
