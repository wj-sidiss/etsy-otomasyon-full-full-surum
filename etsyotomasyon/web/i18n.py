"""Which language the app speaks, and the static string files' self-check.

The strings themselves live in `static/i18n/<namespace>.json` as
`{"tr": {...}, "en": {...}}` and are rendered by the browser. The server only
needs to know the person's language before the page has loaded, and to prove in
the self-test that every namespace has the same keys in both languages.
"""

from __future__ import annotations

import json
import locale
import os
import re
import subprocess
import sys
from pathlib import Path

LANGUAGES = ("tr", "en")
DEFAULT_LANGUAGE = "en"


def normalise(language: object) -> str | None:
    """"tr" / "en" for anything that means one of them, else None."""
    if not isinstance(language, str):
        return None
    code = language.strip().lower()[:2]
    return code if code in LANGUAGES else None


def _mac_language() -> str:
    """The first of macOS's preferred languages, as a two-letter code, or ""."""
    try:
        out = subprocess.run(
            ["defaults", "read", "-g", "AppleLanguages"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"[A-Za-z]{2}", out.partition("(")[2])
    return found.group(0).lower() if found else ""


def detect_language() -> str:
    """Turkish for a Turkish system, English for everything else."""
    if sys.platform == "win32":
        try:
            import ctypes

            # Low 10 bits are the primary language; 0x1F is Turkish.
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x1F:
                return "tr"
            return "en"
        except (AttributeError, OSError):
            pass
    if sys.platform == "darwin":
        # An app opened from Finder gets no LANG; the language the person chose
        # lives in the user defaults instead.
        preferred = _mac_language()
        if preferred:
            return "tr" if preferred == "tr" else "en"
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(name, "")
        if value:
            return "tr" if value.lower().startswith("tr") else "en"
    try:
        current = locale.getlocale()[0] or ""
    except ValueError:
        current = ""
    return "tr" if current.lower().startswith(("tr", "turkish")) else "en"


def check_files(folder: Path) -> list[str]:
    """Problems with the string files in `folder` (static/i18n); empty when all is well.

    Every file must be a JSON object with exactly the languages in LANGUAGES, each a
    flat object (dotted keys, no nesting), and every language must have the same keys.
    """
    problems: list[str] = []
    if not folder.is_dir():
        return problems
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: not valid JSON ({exc})")
            continue
        if not isinstance(data, dict) or sorted(data) != sorted(LANGUAGES):
            problems.append(f"{path.name}: must hold exactly {list(LANGUAGES)}")
            continue
        tables = {code: data[code] for code in LANGUAGES}
        bad = [code for code, table in tables.items() if not isinstance(table, dict)]
        if bad:
            problems.append(f"{path.name}: {', '.join(bad)} is not an object")
            continue
        first = LANGUAGES[0]
        for code in LANGUAGES[1:]:
            only_first = sorted(set(tables[first]) - set(tables[code]))
            only_other = sorted(set(tables[code]) - set(tables[first]))
            if only_first:
                problems.append(f"{path.name}: missing in {code}: {only_first}")
            if only_other:
                problems.append(f"{path.name}: missing in {first}: {only_other}")
        for code, table in tables.items():
            # Keys are flat ("table.status"); a nested object would hide its keys
            # from the comparison above.
            nested = sorted(k for k, v in table.items() if isinstance(v, dict))
            if nested:
                problems.append(f"{path.name}: {code} has nested objects (use flat keys): {nested}")
    return problems
