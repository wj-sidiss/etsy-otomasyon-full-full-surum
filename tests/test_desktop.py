"""The app's single entry point and the settings it keeps.

No arguments starts the web app; `--self-test` checks a build; any other arguments
are the command line tool. The window itself is a web page now: see
tests/test_web_*.py.
"""

from __future__ import annotations

import os

import pytest

import etsyotomasyon.web
from etsyotomasyon import __version__, desktop
from etsyotomasyon.config import write_env_file
from etsyotomasyon.desktop import settings

# --- main() dispatch -----------------------------------------------------------------


def test_no_arguments_start_the_web_app(monkeypatch):
    calls = []
    monkeypatch.setattr(etsyotomasyon.web, "launch", lambda **kw: calls.append(kw) or 0)
    with pytest.raises(SystemExit) as exit_info:
        desktop.main([])
    assert exit_info.value.code == 0
    assert calls == [{}]


def test_the_finder_process_number_is_not_an_argument(monkeypatch):
    calls = []
    monkeypatch.setattr(etsyotomasyon.web, "launch", lambda **kw: calls.append(kw) or 0)
    with pytest.raises(SystemExit):
        desktop.main(["-psn_0_1234567"])
    assert calls == [{}]


def test_self_test_runs_the_web_self_test(monkeypatch):
    monkeypatch.setattr(etsyotomasyon.web, "self_test", lambda: 0)
    with pytest.raises(SystemExit) as exit_info:
        desktop.main(["--self-test"])
    assert exit_info.value.code == 0

    monkeypatch.setattr(etsyotomasyon.web, "self_test", lambda: 1)
    with pytest.raises(SystemExit) as exit_info:
        desktop.main(["--self-test"])
    assert exit_info.value.code == 1


def test_arguments_run_the_command_line_tool(capsys):
    with pytest.raises(SystemExit) as exit_info:
        desktop.main(["--version"])
    assert exit_info.value.code == 0
    assert f"etsyotomasyon {__version__}" in capsys.readouterr().out


def test_a_usage_error_keeps_its_exit_code(capsys):
    with pytest.raises(SystemExit) as exit_info:
        desktop.main(["no-such-command"])
    assert exit_info.value.code == 2


def test_a_library_error_is_a_message_not_a_traceback(capsys):
    with pytest.raises(SystemExit) as exit_info:
        desktop.main(["shop", "info"])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "ETSY_KEYSTRING is not set" in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err


def test_a_crash_at_start_is_reported_not_raised(monkeypatch):
    reports = []

    def boom(**_kw):
        raise RuntimeError("no server today")

    monkeypatch.setattr(etsyotomasyon.web, "launch", boom)
    monkeypatch.setattr(desktop, "_report_crash", reports.append)
    with pytest.raises(SystemExit) as exit_info:
        desktop.main([])
    assert exit_info.value.code == 1
    assert len(reports) == 1 and "no server today" in reports[0]


def test_nothing_escapes_to_the_bootloader():
    def fails() -> int:
        raise ValueError("bad")

    assert desktop._guarded(fails) == 1
    assert desktop._guarded(lambda: 0) == 0

    def exits() -> int:
        raise SystemExit(3)

    assert desktop._guarded(exits) == 3


def test_the_old_self_test_name_still_works(monkeypatch):
    monkeypatch.setattr(etsyotomasyon.web, "self_test", lambda: 0)
    assert desktop.self_test() == 0


def test_no_tk_module_is_left():
    import importlib.util

    for name in ("app", "runner", "i18n"):
        assert importlib.util.find_spec(f"etsyotomasyon.desktop.{name}") is None


# --- settings ---------------------------------------------------------------------


def test_save_merges_into_the_env_file_and_the_running_process():
    write_env_file(settings.env_path(), {"ETSY_SHOP_ID": "5", "ETSY_KEYSTRING": "old"})
    path = settings.save({"ETSY_KEYSTRING": " abc ", "ETSY_SHARED_SECRET": "sec"})

    content = path.read_text(encoding="utf-8")
    assert "ETSY_SHOP_ID=5" in content  # a key the app has no field for survives
    assert "ETSY_KEYSTRING=abc" in content and "=old" not in content
    assert os.environ["ETSY_KEYSTRING"] == "abc"
    assert settings.current("ETSY_SHARED_SECRET") == "sec"

    settings.save({"ETSY_SHARED_SECRET": ""})
    assert "ETSY_SHARED_SECRET" not in settings.env_path().read_text(encoding="utf-8")
    assert "ETSY_SHARED_SECRET" not in os.environ


def test_saved_keys_are_what_the_cli_reads():
    from etsyotomasyon.config import Config

    settings.save({"ETSY_KEYSTRING": "key123", "ETSY_SHARED_SECRET": "sec456",
                   "ETSY_REDIRECT_URI": settings.ETSY_REDIRECT_DEFAULT})
    for key in ("ETSY_KEYSTRING", "ETSY_SHARED_SECRET", "ETSY_REDIRECT_URI"):
        os.environ.pop(key)  # a fresh process: only the file is left
    config = Config.load()
    assert (config.keystring, config.shared_secret) == ("key123", "sec456")
    assert config.redirect_uri == settings.ETSY_REDIRECT_DEFAULT


def test_prefs_round_trip_and_survive_a_corrupt_file():
    settings.save_app_prefs({"language": "tr"})
    settings.save_shop_prefs({"workspace": "C:/x"})
    assert settings.load_app_prefs() == {"language": "tr"}
    assert settings.load_shop_prefs() == {"workspace": "C:/x"}
    settings.app_prefs_path().write_text("{not json", encoding="utf-8")
    assert settings.load_app_prefs() == {}


def test_each_shop_has_its_own_keys_and_preferences():
    from etsyotomasyon import shops

    settings.save({"ETSY_KEYSTRING": "first-key", "ETSY_SHARED_SECRET": "first-secret"})
    settings.save_shop_prefs({"workspace": "first-folder"})
    second = shops.add()

    settings.use_shop(second.id)
    assert settings.current("ETSY_KEYSTRING") == ""  # nothing leaks from the first shop
    assert settings.load_shop_prefs() == {}
    settings.save({"ETSY_KEYSTRING": "second-key", "ETSY_SHARED_SECRET": "second-secret"})
    assert (second.home / ".env").is_file()

    settings.use_shop("")
    assert settings.current("ETSY_KEYSTRING") == "first-key"
    assert settings.load_shop_prefs() == {"workspace": "first-folder"}
    settings.use_shop(second.id)
    assert settings.current("ETSY_KEYSTRING") == "second-key"


def test_the_app_icon_renders_without_a_display():
    from etsyotomasyon.desktop import icon

    image = icon.render(64)
    assert image.size == (64, 64)

