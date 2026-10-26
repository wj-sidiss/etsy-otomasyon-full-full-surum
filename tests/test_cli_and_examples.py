"""Regression guards for two defects that shipped in 0.1.0.

Both were found only by running the tool on a real Windows machine, not by the unit
suite, so they get explicit tests here. The commands at the end (`listings pull`,
`seo suggest`) run against an httpx.MockTransport; nothing reaches the network.
"""

import time
import urllib.parse
from fnmatch import fnmatch
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from etsyotomasyon import auth, cli
from etsyotomasyon.cli import app
from etsyotomasyon.client import EtsyClient
from etsyotomasyon.config import Config, split_credential, write_env_file
from etsyotomasyon.csvio import read_rows
from etsyotomasyon.listings import LISTING_COLUMNS, build_payload
from etsyotomasyon.orders import ORDER_COLUMNS

REPO = Path(__file__).resolve().parents[1]


# --- the bundled example must satisfy its own documented quickstart ------------


def test_example_listings_csv_exists_and_has_the_documented_header():
    rows = read_rows(REPO / "examples" / "listings.csv")
    assert rows, "examples/listings.csv has no data rows"
    for column in LISTING_COLUMNS:
        assert column in rows[0], f"example CSV is missing the {column!r} column"


def test_every_row_of_the_example_csv_passes_validation():
    # The README tells people to run `listings push examples/listings.csv --dry-run`
    # as their first command, so every row of it has to pass.
    rows = read_rows(REPO / "examples" / "listings.csv")
    for index, row in enumerate(rows, start=2):
        payload = build_payload(row, is_update=False)
        assert payload["title"]
        assert payload["shipping_profile_id"], f"row {index} has no shipping_profile_id"


def test_the_files_column_sits_after_images_and_is_empty_for_physical_rows():
    assert LISTING_COLUMNS.index("files") == LISTING_COLUMNS.index("images") + 1
    rows = read_rows(REPO / "examples" / "listings.csv")
    assert all(row["type"] == "physical" and row["files"] == "" for row in rows)
    # A CSV written before the column existed still pushes exactly as before.
    old = {k: v for k, v in rows[0].items() if k != "files"}
    assert build_payload(old, is_update=False) == build_payload(rows[0], is_update=False)


def test_the_listings_template_command_writes_the_files_column():
    result = CliRunner().invoke(app, ["listings", "template", "-o", "t.csv"])
    assert result.exit_code == 0, result.output
    rows = read_rows(Path("t.csv"))
    assert list(rows[0]) == LISTING_COLUMNS and rows[0]["files"] == ""
    assert "files" in result.output


def test_example_tracking_csv_has_the_columns_orders_ship_needs():
    rows = read_rows(REPO / "examples" / "tracking.csv")
    assert rows
    for column in ("receipt_id", "tracking_code", "carrier_name"):
        assert column in rows[0]


# --- console encoding ----------------------------------------------------------


def test_symbol_falls_back_when_the_stream_cannot_encode_it(monkeypatch):
    # cp1254 is the default on a Turkish Windows install and has no U+2713. Before
    # the fix, every command that printed one died with UnicodeEncodeError as soon as
    # output was redirected.
    class FakeStream:
        encoding = "cp1254"

    monkeypatch.setattr(cli.sys, "stdout", FakeStream())
    assert cli._symbol("✓", "OK") == "OK"
    assert cli._symbol("-", "-") == "-"


def test_symbol_keeps_the_glyph_on_a_utf8_stream(monkeypatch):
    class FakeStream:
        encoding = "utf-8"

    monkeypatch.setattr(cli.sys, "stdout", FakeStream())
    assert cli._symbol("✓", "OK") == "✓"


def test_symbol_survives_a_stream_with_no_encoding(monkeypatch):
    class FakeStream:
        encoding = None

    monkeypatch.setattr(cli.sys, "stdout", FakeStream())
    assert cli._symbol("✓", "OK") == "OK"


def test_force_utf8_ignores_streams_that_cannot_be_reconfigured():
    class Stubborn:
        def reconfigure(self, **_kw):
            raise ValueError("nope")

    cli._force_utf8(Stubborn())  # must not raise
    cli._force_utf8(object())  # no reconfigure attribute at all


def test_status_markers_are_encodable_by_the_current_stdout():
    # Whatever the markers resolved to at import time, printing them must be safe.
    encoding = getattr(cli.sys.stdout, "encoding", None) or "utf-8"
    for marker in (cli.TICK, cli.CROSS, cli.BULLET):
        marker.encode(encoding)


# --- exports must not be committable -------------------------------------------


def _gitignore_patterns():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def test_orders_export_carries_personal_data():
    # This is the reason the CSV rule below exists. If these columns ever go away,
    # the guard can be relaxed — until then it must hold.
    for column in ("buyer_name", "buyer_email", "ship_address", "ship_zip"):
        assert column in ORDER_COLUMNS


@pytest.mark.parametrize(
    "filename",
    [
        "orders.csv",           # `orders pull` default
        "listings.csv",         # `listings template` default
        "listings-export.csv",  # `listings pull` default
        "results.csv",          # `listings push --out` in the README
        "seo-report.csv",       # `seo audit --out` in the README
        "to-ship.csv",          # README example
        "musteri-listesi.csv",  # anything a seller names themselves
    ],
)
def test_every_export_filename_is_gitignored(filename):
    # The README tells people to clone the repo and run commands inside it, so an
    # un-ignored export is one `git add .` away from publishing a customer list.
    patterns = _gitignore_patterns()
    assert any(fnmatch(filename, p) for p in patterns if not p.startswith("!"))


def test_the_shipped_examples_are_still_tracked():
    patterns = _gitignore_patterns()
    negations = [p[1:] for p in patterns if p.startswith("!")]
    for example in ("examples/listings.csv", "examples/tracking.csv"):
        assert any(fnmatch(example, n) for n in negations), f"{example} would be ignored"


# --- `etsyotomasyon init` -------------------------------------------------------------

runner = CliRunner()

INIT_ARGS = ["--keystring", "KEY123", "--redirect-uri", "http://localhost:3003/cb", "--no-check"]


def test_split_credential_handles_the_joined_header_form():
    assert split_credential("KEY:SECRET") == ("KEY", "SECRET")
    assert split_credential("KEY", "SECRET") == ("KEY", "SECRET")
    assert split_credential("  KEY  ", "  SECRET  ") == ("KEY", "SECRET")


def test_split_credential_prefers_an_explicit_secret():
    assert split_credential("KEY:FROMFIELD", "EXPLICIT") == ("KEY", "EXPLICIT")


def test_write_env_file_skips_empty_values(tmp_path):
    path = tmp_path / ".env"
    write_env_file(path, {"A": "1", "B": "", "C": "3"})
    text = path.read_text(encoding="utf-8")
    assert "A=1" in text and "C=3" in text and "B=" not in text


def test_init_writes_all_three_settings(tmp_path):
    env = tmp_path / ".env"
    result = runner.invoke(app, ["init", *INIT_ARGS, "--path", str(env)], input="SECRET99\n")
    assert result.exit_code == 0, result.output
    text = env.read_text(encoding="utf-8")
    assert "ETSY_KEYSTRING=KEY123" in text
    assert "ETSY_SHARED_SECRET=SECRET99" in text
    assert "ETSY_REDIRECT_URI=http://localhost:3003/cb" in text


def test_init_never_echoes_the_secret(tmp_path):
    env = tmp_path / ".env"
    result = runner.invoke(app, ["init", *INIT_ARGS, "--path", str(env)], input="SECRET99\n")
    assert "SECRET99" not in result.output


def test_init_refuses_to_clobber_an_existing_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ETSY_KEYSTRING=existing\n", encoding="utf-8")
    result = runner.invoke(app, ["init", *INIT_ARGS, "--path", str(env)], input="SECRET99\n")
    assert result.exit_code == 1
    assert env.read_text(encoding="utf-8") == "ETSY_KEYSTRING=existing\n"


def test_init_overwrites_with_force(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ETSY_KEYSTRING=existing\n", encoding="utf-8")
    result = runner.invoke(
        app, ["init", *INIT_ARGS, "--path", str(env), "--force"], input="SECRET99\n"
    )
    assert result.exit_code == 0, result.output
    assert "KEY123" in env.read_text(encoding="utf-8")


def test_init_rejects_an_ip_callback(tmp_path):
    env = tmp_path / ".env"
    result = runner.invoke(
        app,
        ["init", "--keystring", "K", "--redirect-uri", "http://127.0.0.1:3003/cb",
         "--no-check", "--path", str(env)],
        input="SECRET99\n",
    )
    assert result.exit_code != 0
    assert not env.exists()


# --- commands that read listings ---------------------------------------------------

SHOP = 12345678
PREFIX = "/v3/application"


def _fake_etsy(monkeypatch, responses):
    """Point the CLI's client at a MockTransport answering by path; returns the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = responses.get(request.url.path[len(PREFIX):])
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=body)

    def make(**_kw):
        config = Config(keystring="KEY123", shared_secret="SECRET456", shop_id=SHOP,
                        rate_per_sec=10)
        token = auth.Token("1.access", "1.refresh", time.time() + 3600, ("listings_r",))
        return EtsyClient(config, token=token, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "_client", make)
    return seen


def test_listings_pull_has_no_views_column(monkeypatch):
    # Etsy's ShopListing has no view count, so a "views" column was always empty.
    _fake_etsy(monkeypatch, {f"/shops/{SHOP}/listings": {"count": 1, "results": [
        {"listing_id": 1000001, "title": "Example Mug", "state": "active",
         "url": "https://www.etsy.com/listing/1000001/example-mug", "num_favorers": 4},
    ]}})
    result = runner.invoke(app, ["listings", "pull", "--out", "export.csv"])
    assert result.exit_code == 0, result.output
    header = Path("export.csv").read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert "views" not in header
    assert header[-2:] == ["url", "num_favorers"]
    row = read_rows(Path("export.csv"))[0]
    assert row["num_favorers"] == "4" and row["listing_id"] == "1000001"


def test_seo_suggest_audits_the_listing_as_plain_text(monkeypatch):
    # Etsy sends the seller's text HTML-escaped. Read raw, the tag below is 21 characters
    # (over Etsy's 20) and the title shows "&#39;"; decoded, neither is a problem, and
    # the listing's own tag is not suggested back to it.
    seen = _fake_etsy(monkeypatch, {
        "/listings/1000001": {
            "listing_id": 1000001, "title": "Mom&#39;s Coffee Mug &amp; Gift",
            "tags": ["mother&#39;s day gift"], "description": "", "state": "active",
        },
        "/listings/active": {"count": 2, "results": [
            {"listing_id": 1000101, "title": "Mother&#39;s Day Mug",
             "tags": ["mother&#39;s day gift", "mom mug"]},
            {"listing_id": 1000102, "title": "Coffee Mug for Mom",
             "tags": ["mother&#39;s day gift", "mom mug"]},
        ]},
    })
    result = runner.invoke(app, ["seo", "suggest", "1000001"])
    assert result.exit_code == 0, result.output
    assert "Mom's Coffee Mug & Gift" in result.output
    assert "&#39;" not in result.output and "&amp;" not in result.output
    assert "tags.too_long" not in result.output
    assert "mom mug" in result.output  # a real suggestion
    assert "mother's day gift" not in result.output.split("free tag slot")[-1]
    listing_request = next(r for r in seen if r.url.path == f"{PREFIX}/listings/1000001")
    assert listing_request.headers["Authorization"] == "Bearer 1.access"  # drafts too
    search = next(r for r in seen if r.url.path == f"{PREFIX}/listings/active")
    keywords = dict(urllib.parse.parse_qsl(search.url.query.decode()))["keywords"]
    assert keywords == "Mom's Coffee Mug &"
