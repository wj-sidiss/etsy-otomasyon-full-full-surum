"""The two languages: every string file has both, with the same keys, and the app
picks the person's language before anything has loaded."""

from __future__ import annotations

import json

import pytest

from etsyotomasyon.web import i18n
from etsyotomasyon.web.server import STATIC_DIR

I18N_DIR = STATIC_DIR / "i18n"
FILES = sorted(I18N_DIR.glob("*.json")) if I18N_DIR.is_dir() else []


@pytest.mark.skipif(not FILES, reason="the UI's string files are not there yet")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_string_file_has_the_same_keys_in_turkish_and_english(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(data) == ["en", "tr"], path.name
    missing_in_en = sorted(set(data["tr"]) - set(data["en"]))
    missing_in_tr = sorted(set(data["en"]) - set(data["tr"]))
    assert not missing_in_en, f"{path.name}: only in tr: {missing_in_en}"
    assert not missing_in_tr, f"{path.name}: only in en: {missing_in_tr}"


@pytest.mark.skipif(not FILES, reason="the UI's string files are not there yet")
def test_the_self_test_check_agrees():
    assert i18n.check_files(I18N_DIR) == []


def write(folder, name, data) -> None:
    (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_check_files_finds_what_is_wrong(tmp_path):
    write(tmp_path, "good.json", {"tr": {"a": "bir", "b.c": "iki"}, "en": {"a": "one", "b.c": "two"}})
    assert i18n.check_files(tmp_path) == []
    write(tmp_path, "uneven.json", {"tr": {"a": "bir", "x": "fazla"}, "en": {"a": "one"}})
    write(tmp_path, "one-language.json", {"tr": {}})
    write(tmp_path, "nested.json", {"tr": {"a": {"b": "c"}}, "en": {"a": {"b": "c"}}})
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    problems = "\n".join(i18n.check_files(tmp_path))
    assert "uneven.json: missing in en: ['x']" in problems
    assert "one-language.json" in problems
    assert "nested.json" in problems
    assert "broken.json: not valid JSON" in problems
    assert "good.json" not in problems


def test_a_missing_folder_has_no_problems(tmp_path):
    assert i18n.check_files(tmp_path / "nowhere") == []


@pytest.mark.parametrize("value, expected", [
    ("tr", "tr"), ("en", "en"), ("TR", "tr"), ("en-GB", "en"), ("de", None), (None, None), (1, None),
])
def test_normalise(value, expected):
    assert i18n.normalise(value) == expected


def test_language_detection_reads_the_locale(monkeypatch):
    monkeypatch.setattr(i18n.sys, "platform", "linux")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    assert i18n.detect_language() == "tr"
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert i18n.detect_language() == "en"


def test_a_mac_opened_from_finder_follows_the_system_language(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(i18n.sys, "platform", "darwin")
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    answer = '(\n    "tr-TR",\n    "en-GB"\n)\n'
    monkeypatch.setattr(i18n.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a, 0, stdout=answer, stderr=""))
    assert i18n.detect_language() == "tr"
    answer = '(\n    "de-DE"\n)\n'
    assert i18n.detect_language() == "en"


def test_the_saved_language_wins_over_the_system(web, monkeypatch):
    monkeypatch.setattr(i18n, "detect_language", lambda: "tr")
    assert web.client.get("/api/session").json()["language"] == "tr"
    web.client.post("/api/prefs", json={"language": "en"})
    assert web.client.get("/api/session").json()["language"] == "en"


NOTIFY_CALL = __import__("re").compile(r"""\.notify\(\s*["']([\w-]+)["']\s*,\s*["']([\w.]+)["']""")


@pytest.mark.skipif(not FILES, reason="the UI's string files are not there yet")
def test_every_notification_key_in_the_code_has_a_string():
    # The bell shows t(ns + ":" + key); a key without its string shows as raw text
    # (orders:print_missing was shown that way until 0.3.3).
    package = STATIC_DIR.parent.parent
    missing = []
    for source in sorted(package.rglob("*.py")):
        for ns, key in NOTIFY_CALL.findall(source.read_text(encoding="utf-8")):
            path = I18N_DIR / f"{ns}.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            for lang in ("tr", "en"):
                if key not in data.get(lang, {}):
                    missing.append(f"{source.name}: {ns}:{key} ({lang})")
    assert not missing, missing
