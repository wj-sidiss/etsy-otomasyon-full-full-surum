"""Reading the clipboard, with no third-party dependency.

`etsyotomasyon init --from-clipboard` exists so a secret can go from the page you copied
it on straight into your `.env`, without passing through your shell history, your
screen, or anyone helping you set the tool up.

The value is never printed. Callers get the string and are expected to keep it that
way — everything in etsyotomasyon reports a length, never a secret.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .errors import ValidationError

# Longest sensible paste. A clipboard holding a document is a mistake, not a secret.
MAX_LENGTH = 4096


def _from_tk() -> str | None:
    """Tkinter is in the standard library and works on all three platforms."""
    try:
        import tkinter
    except ImportError:
        return None
    try:
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001 — no display, no Tk, empty clipboard: all the same here
        return None


def _from_command() -> str | None:
    """Platform fallbacks, for a headless Linux box or a Python built without Tk."""
    if sys.platform == "darwin":
        candidates = [["pbpaste"]]
    elif sys.platform == "win32":
        candidates = [["powershell", "-NoProfile", "-Command", "Get-Clipboard"]]
    else:
        candidates = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ]

    for command in candidates:
        if not shutil.which(command[0]):
            continue
        try:
            done = subprocess.run(  # noqa: S603 — fixed command names, no user input
                command, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return done.stdout
    return None


def read_text() -> str:
    """Return the clipboard's text, stripped. Raises if it cannot be read or is empty."""
    for source in (_from_tk, _from_command):
        raw = source()
        if raw is None:
            continue
        text = raw.strip()
        if not text:
            raise ValidationError(
                "The clipboard is empty. Copy the value first, then run this again."
            )
        if len(text) > MAX_LENGTH:
            raise ValidationError(
                f"The clipboard holds {len(text)} characters — that is not a credential. "
                "Copy just the value and try again."
            )
        if "\n" in text:
            raise ValidationError(
                "The clipboard holds more than one line. Copy just the value and try again."
            )
        return text

    raise ValidationError(
        "Could not read the clipboard on this system.\n"
        "Run `etsyotomasyon init` without --from-clipboard and paste at the prompt instead."
    )
