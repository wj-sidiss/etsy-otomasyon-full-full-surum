"""Reading a secret from the clipboard.

`init --from-clipboard` exists so a credential can go from the page you copied it on
straight into your .env — not through your shell history, not across your screen, and
not through anyone helping you set the tool up. These tests pin the guard rails and,
most importantly, that the value is never printed.
"""

import pytest
from typer.testing import CliRunner

from etsyotomasyon import clipboard
from etsyotomasyon.cli import app
from etsyotomasyon.errors import ValidationError

runner = CliRunner()


def _stub(monkeypatch, value):
    monkeypatch.setattr(clipboard, "_from_tk", lambda: value)
    monkeypatch.setattr(clipboard, "_from_command", lambda: None)


def test_a_normal_value_is_returned_stripped(monkeypatch):
    _stub(monkeypatch, "  x1abc2def3  \n")
    assert clipboard.read_text() == "x1abc2def3"


def test_an_empty_clipboard_says_to_copy_first(monkeypatch):
    _stub(monkeypatch, "   ")
    with pytest.raises(ValidationError, match="clipboard is empty"):
        clipboard.read_text()


def test_a_multiline_clipboard_is_refused(monkeypatch):
    # Pasting a whole page into a secret field would write nonsense to .env.
    _stub(monkeypatch, "line one\nline two")
    with pytest.raises(ValidationError, match="more than one line"):
        clipboard.read_text()


def test_an_enormous_clipboard_is_refused(monkeypatch):
    _stub(monkeypatch, "x" * (clipboard.MAX_LENGTH + 1))
    with pytest.raises(ValidationError, match="not a credential"):
        clipboard.read_text()


def test_an_unreadable_clipboard_points_at_the_prompt(monkeypatch):
    monkeypatch.setattr(clipboard, "_from_tk", lambda: None)
    monkeypatch.setattr(clipboard, "_from_command", lambda: None)
    with pytest.raises(ValidationError, match="without --from-clipboard"):
        clipboard.read_text()


def test_the_command_fills_the_secret_from_the_clipboard(monkeypatch, tmp_path):
    _stub(monkeypatch, "SECRETVAL9")
    env = tmp_path / ".env"
    result = runner.invoke(
        app,
        ["init", "--keystring", "KEY123", "--redirect-uri", "http://localhost:3003/cb",
         "--no-check", "--path", str(env), "--from-clipboard"],
    )
    assert result.exit_code == 0, result.output
    assert "ETSY_SHARED_SECRET=SECRETVAL9" in env.read_text(encoding="utf-8")


def test_the_secret_is_never_printed(monkeypatch, tmp_path):
    _stub(monkeypatch, "SECRETVAL9")
    env = tmp_path / ".env"
    result = runner.invoke(
        app,
        ["init", "--keystring", "KEY123", "--redirect-uri", "http://localhost:3003/cb",
         "--no-check", "--path", str(env), "--from-clipboard"],
    )
    assert "SECRETVAL9" not in result.output
    assert "read 10 characters" in result.output


def test_a_colon_joined_paste_is_split(monkeypatch, tmp_path):
    # Someone copies the whole `keystring:shared_secret` header value.
    _stub(monkeypatch, "SECRETVAL9")
    env = tmp_path / ".env"
    runner.invoke(
        app,
        ["init", "--keystring", "KEY123:FROMFIELD", "--redirect-uri",
         "http://localhost:3003/cb", "--no-check", "--path", str(env), "--from-clipboard"],
    )
    text = env.read_text(encoding="utf-8")
    assert "ETSY_KEYSTRING=KEY123" in text
    assert "ETSY_SHARED_SECRET=SECRETVAL9" in text, "an explicit secret wins over the split"
