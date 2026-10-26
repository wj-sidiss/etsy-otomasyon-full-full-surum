"""The shell and the Panel as the video draws them: the runs log behind the four KPI cards,
the Pinterest nav flag, and the static pieces (routes, icons, logo, tokens, texts)."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from etsyotomasyon import seo
from etsyotomasyon.web.api import core, dashboard

STATIC = Path(__file__).resolve().parents[1] / "etsyotomasyon" / "web" / "static"

TAGS = ["retro sunset shirt", "mountain tshirt", "hiking shirt", "nature lover gift", "camping tee",
        "outdoor shirt", "vintage mountain", "adventure shirt", "hiker gift", "national park tee",
        "sunset tshirt", "gift for hiker", "wanderlust shirt"]
TITLE = "Retro Mountain Sunset Shirt, Vintage Hiking T-Shirt, Nature Lover Gift"


def _last_run(root: Path, *, job_id="job1", batch="2026-09-01_1000", finished=None, dry_run=False,
              created=2, duration=16.0) -> dict:
    finished = finished if finished is not None else time.time()
    items = [
        {"index": 0, "name": "a.png", "status": "ok", "listing_id": 1000001, "title": TITLE, "tags": TAGS},
        {"index": 1, "name": "b.png", "status": "partial", "listing_id": 1000002, "title": "Mug", "tags": []},
        {"index": 2, "name": "c.png", "status": "error", "listing_id": None, "title": "", "tags": []},
    ]
    data = {
        "version": 1,
        "job_id": job_id,
        "summary": {"batch": batch, "dry_run": dry_run, "total": 3, "created": created, "errors": 1,
                    "started_at": finished - duration, "finished_at": finished, "duration": duration},
        "items": items,
    }
    drafts = root / "3-DRAFTS"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "last-run.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _run(finished: datetime, created: int, *, duration=None, seo_scores=(), shop=None, key=None) -> dict:
    return {"key": key or f"k{finished.timestamp()}", "shop": shop, "finished_at": finished.timestamp(),
            "duration": duration if duration is not None else created * 8.0, "created": created,
            "total": created, "errors": 0, "seo": list(seo_scores)}


# --- the runs log ----------------------------------------------------------------------------


def test_a_finished_run_is_logged_once_with_its_drafts_seo(tmp_path):
    _last_run(tmp_path)
    assert dashboard.record_last_run(tmp_path, "12345678") is True
    assert dashboard.record_last_run(tmp_path, "12345678") is False  # the same run again
    runs = dashboard.load_runs(tmp_path / "3-DRAFTS")
    assert len(runs) == 1
    run = runs[0]
    assert run["shop"] == "12345678" and run["created"] == 2 and run["duration"] == 16.0
    # Only the drafts that exist are scored (ok and partial with a listing id), title+tags.
    expected = [seo.audit_listing({"title": TITLE, "tags": TAGS}).score,
                seo.audit_listing({"title": "Mug", "tags": []}).score]
    assert run["seo"] == expected


def test_a_test_run_is_not_work(tmp_path):
    _last_run(tmp_path, dry_run=True)
    assert dashboard.record_last_run(tmp_path, None) is False
    assert not (tmp_path / "3-DRAFTS" / "runs.json").exists()


def test_a_broken_last_run_is_skipped(tmp_path):
    drafts = tmp_path / "3-DRAFTS"
    drafts.mkdir()
    (drafts / "last-run.json").write_text("{not json", encoding="utf-8")
    assert dashboard.record_last_run(tmp_path, None) is False
    (drafts / "last-run.json").write_text(json.dumps({"summary": {"dry_run": False}}), encoding="utf-8")
    assert dashboard.record_last_run(tmp_path, None) is False


def test_the_template_counts_in_the_score(tmp_path):
    from etsyotomasyon.drop.template import Template
    from etsyotomasyon.drop.workspace import Workspace

    ws = Workspace(tmp_path)
    ws.create()
    description = "A soft vintage hiking shirt with a retro mountain sunset. " * 5
    template = Template(source_listing_id=1000013, source_title="Example", fields={},
                        materials=["cotton"], description=description, tags=[])
    ws.write_template(template.to_dict())
    _last_run(tmp_path)
    assert dashboard.record_last_run(tmp_path, None)
    run = dashboard.load_runs(ws.drafts)[0]
    alone = seo.audit_listing({"title": TITLE, "tags": TAGS}).score
    with_template = seo.audit_listing({"title": TITLE, "tags": TAGS, "description": description,
                                       "materials": ["cotton"]}).score
    assert run["seo"][0] == with_template > alone


def test_the_four_cards_count_this_month_today_and_the_week(web):
    ctx = web.ctx
    drafts = ctx.workspace().drafts
    now = datetime(2026, 9, 28, 15, 0)
    runs = [
        _run(datetime(2026, 8, 30, 12), 40),  # last month: only the bars' week may see it
        _run(datetime(2026, 9, 2, 12), 101, seo_scores=[80] * 3),
        _run(datetime(2026, 9, 22, 12), 17),
        _run(datetime(2026, 9, 27, 12), 39, duration=39 * 10.0),
        _run(datetime(2026, 9, 28, 9), 50, seo_scores=[90, 100]),
        _run(datetime(2026, 9, 28, 10), 0, duration=30),  # made nothing: no time per product
        _run(datetime(2026, 9, 28, 11), 5, shop="99999999"),  # another shop's run
    ]
    dashboard.save_runs(drafts, runs)
    work = dashboard.work_summary(ctx, now=now)
    assert work["month"] == "2026-09"
    assert work["drafts_month"] == 101 + 17 + 39 + 50
    assert work["drafts_today"] == 50
    assert work["daily"] == [17, 0, 0, 0, 0, 39, 50]
    assert len(work["cumulative"]) == 28 and work["cumulative"][0] == 0
    assert work["cumulative"][1] == 101 and work["cumulative"][-1] == 207
    made = 40 + 101 + 17 + 39 + 50
    assert work["seconds_per_item"] == round((made * 8 + 39 * 2) / made, 1)
    assert work["manual_minutes"] == 20 and work["minutes_saved"] == 207 * 20
    assert work["seo_avg"] == round((90 + 100 + 80 * 3) / 5) and work["seo_sample"] == 5


def test_an_empty_shop_shows_nothing_yet(web):
    work = web.client.get("/api/dashboard").json()["work"]
    assert work["drafts_month"] == 0 and work["drafts_today"] == 0
    assert work["daily"] == [0] * 7
    assert work["seconds_per_item"] is None and work["seo_avg"] is None
    assert work["minutes_saved"] == 0


def test_the_panel_logs_a_run_it_missed(web):
    ws = web.ctx.workspace()
    _last_run(ws.root)
    work = web.client.get("/api/dashboard").json()["work"]
    assert work["drafts_month"] == 2 and work["drafts_today"] == 2
    assert len(dashboard.load_runs(ws.drafts)) == 1


def test_a_designs_job_is_logged_when_it_ends(web):
    ctx = web.ctx
    ws = ctx.workspace()

    def work(job):
        ctx.changed("listings", source="designs")  # as the designs run does while it runs
        time.sleep(0.2)
        _last_run(ws.root, job_id=job.id)
        return {"created": 2}

    job = ctx.jobs.start("designs", "designs:job.title", work)
    assert job.wait(10)
    deadline = time.monotonic() + 5
    while not dashboard.load_runs(ws.drafts) and time.monotonic() < deadline:
        time.sleep(0.02)
    runs = dashboard.load_runs(ws.drafts)
    assert len(runs) == 1 and runs[0]["key"].startswith(job.id + ":")


def test_the_log_keeps_the_newest_runs(tmp_path):
    base = datetime(2026, 1, 1)
    runs = [_run(base + timedelta(minutes=i), 1, key=f"r{i}") for i in range(dashboard.RUNS_KEEP + 5)]
    dashboard.save_runs(tmp_path, runs)
    kept = dashboard.load_runs(tmp_path)
    assert len(kept) == dashboard.RUNS_KEEP and kept[0]["key"] == "r5"


# --- the Pinterest nav item ----------------------------------------------------------------------


def test_the_session_says_whether_pinterest_is_in_use(web):
    from etsyotomasyon import pinterest

    assert web.client.get("/api/session").json()["pinterest"] is False
    queue = pinterest.Queue.load()
    queue.add([{"listing_id": 1000001, "rank": 1, "payload": {"board_id": "900", "title": "t"}}],
              start=datetime.now().date(), per_day=2)
    queue.save()
    assert web.client.get("/api/session").json()["pinterest"] is True
    assert core.pinterest_in_use() is True


# --- the static shell ----------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


def test_a_draft_has_its_own_address():
    app = _read("js/app.js")
    assert '{ path: "/ilanlar/taslak/:id", page: "listing-detail", nav: "listings" }' in app
    # The tab shows the page's name only.
    assert "document.title = plain || \"etsyotomasyon\";" in app


def test_pinterest_is_an_optional_nav_item():
    app = _read("js/app.js")
    assert re.search(r'\{ page: "pinterest", path: "/pinterest", icon: "pin", optional: true \}', app)
    assert "state.session.pinterest" in app


def test_the_video_palette_and_fonts():
    css = _read("css/base.css")
    for token in ("--bg: #0a0c11;", "--sidebar: #11141b;", "--panel: #11141b;", "--accent: #7b6cff;",
                  "--accent-2: #a89cff;", "--success: #2fd6a3;", "--info: #5ab8ff;",
                  "--border-soft: #1d2230;", "--radius-lg: 14px;", "--topbar-h: 67px;"):
        assert token in css, token
    assert '--font-display: "Plus Jakarta Sans", var(--font);' in css
    # Inter's alternate "a" and digits are not the video's.
    assert "cv11" not in css and "ss01" not in css
    # One violet everywhere: no rule keeps the old one.
    assert not re.search(r"124,\s*108,\s*246", css)
    assert not re.search(r"124,\s*108,\s*246", _read("css/pages/panel.css"))
    assert ".btn-soft {" in css and ".btn-icon.topbar-bell {" in css


def test_the_logo_is_the_videos():
    icons = _read("js/icons.js")
    assert 'viewBox", "2 2 28 28"' in icons
    assert 'fill="none" stroke="#fff" stroke-opacity=".55" stroke-width="2"' in icons
    assert "linearGradient" not in icons.split("export function logoMark")[1]


def test_the_icons_use_the_videos_paths():
    icons = _read("js/icons.js")
    assert "home: '<path d=\"M3 11.5 12 4l9 7.5M5.5 9.5V20h13V9.5\"/>'," in icons
    assert "chart: '<path d=\"M4 20V10M10 20V4M16 20v-7M21 20H3\"/>'," in icons
    assert "zap: '<path d=\"M13 2.5 4.5 13.5H12l-1 8 8.5-11H12l1-8Z\"/>'," in icons
    # Icon.tsx's tag, lock, info, alert, refresh, user, mail, coins and grid.
    for name, markup in {
        "tag": '<path d="M3.5 12.5V4.5h8l9 9-8 8-9-9Z"/><circle cx="8" cy="9" r="1.4"/>',
        "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
        "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.01"/>',
        "alert": '<path d="M12 3.5 2.5 20h19L12 3.5Z"/><path d="M12 10v4.5M12 17.3v.01"/>',
        "refresh": '<path d="M20 11a8 8 0 0 0-14.7-3.8L4 9"/><path d="M4 4v5h5M4 13a8 8 0 0 0 14.7 3.8L20 15"/>'
                   '<path d="M20 20v-5h-5"/>',
        "user": '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
        "mail": '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
        "coins": '<ellipse cx="9" cy="7" rx="5.5" ry="2.5"/>'
                 '<path d="M3.5 7v4c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5V7"/>'
                 '<path d="M9.5 16.3c.9 1 2.9 1.7 5.1 1.7 3 0 5.5-1.1 5.5-2.5v-4c0-1.2-1.7-2.2-4.1-2.4"/>',
        "grid": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
                '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    }.items():
        assert f"  {name}: '{markup}'," in icons, name
    # The video's "sparkle" burst, also drawn wherever the pages ask for "loader".
    burst = '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>'
    assert f"const SPARKLE = '{burst}';" in icons
    assert "  sparkle: SPARKLE," in icons and "  loader: SPARKLE," in icons
    # The cog stays: the video never draws "settings", and its sun-like glyph would not read as one.
    assert "  settings: '<path d=\"M9.87 5.02" in icons


def _rule(css: str, selector: str) -> str:
    """The declarations of the one rule whose selector list ends with `selector`."""
    match = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{\n(.*?)\n\}", css.replace("\r\n", "\n"), re.S)
    assert match, selector
    return match.group(1)


def test_only_the_videos_font_weights():
    # fonts.ts loads Inter 400-700 and Plus Jakarta Sans 600-800: no in-between weights.
    for rel in ("css/base.css", "css/pages/panel.css"):
        # An @font-face rule names the range its variable file draws (Inter 100 900).
        rules = re.sub(r"@font-face\s*\{[^}]*\}", "", _read(rel))
        weights = set(re.findall(r"font-weight:\s*(\d+)", rules))
        assert weights <= {"400", "500", "600", "700", "800"}, (rel, weights)
    css = _read("css/base.css")
    for selector, weight in ((".stat-label", 500), (".field-label", 600), (".tbl th", 600), (".tab", 500),
                             (".tab.is-active", 600), (".tab-count", 700), (".chip", 500), (".chip.is-active", 600),
                             (".step-circle", 700), (".step-label", 600), (".section-title h3", 700)):
        assert f"font-weight: {weight};" in _rule(css, selector), selector
    assert "font-weight: 700;" in _rule(_read("css/pages/panel.css"), ".setup-tile-title")


def test_weight_500_is_not_drawn_as_semibold():
    """Windows' "Segoe UI" has no medium face, so 500 fell back to Semibold (the same as 600).
    Segoe UI Variable, taken through its weight axis, comes first; local() only."""
    css = _read("css/base.css").replace("\r\n", "\n")
    face = re.search(r'@font-face \{\n  font-family: "etsyotomasyon UI";\n(.*?)\n\}', css, re.S)
    assert face, "the local variable face"
    assert 'src: local("Segoe UI Variable Text");' in face.group(1)
    assert "font-weight: 400 700;" in face.group(1) and "url(" not in face.group(1)
    assert '--font: "Inter", "etsyotomasyon UI", system-ui, -apple-system, "Segoe UI", sans-serif;' in css


def test_every_file_the_stylesheets_name_exists():
    for rel in ("css/base.css", "css/pages/panel.css"):
        for ref in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", _read(rel)):
            if ref.startswith(("data:", "#")):
                continue
            assert ((STATIC / rel).parent / ref).resolve().is_file(), (rel, ref)


def test_the_outlined_button_is_the_videos_ghost():
    css = _read("css/base.css")
    rule = _rule(css, ".btn-secondary")
    assert "background: transparent;" in rule and "border-color: var(--border);" in rule
    assert "color: var(--text-2);" in rule
    hover = _rule(css, ".btn-secondary.is-active")
    assert "border-color: var(--border-2);" in hover and "color: var(--text);" in hover


def test_the_not_connected_dot_is_darker_than_its_text():
    assert "background: var(--muted-2);" in _rule(_read("css/base.css"), ".shop-state.tone-muted .dot")


def test_the_setup_tiles_carry_the_videos_steps():
    panel = json.loads(_read("i18n/panel.json"))
    tr, en = panel["tr"], panel["en"]
    assert (tr["setup.shop"], tr["setup.mockups"], tr["setup.designs"]) == (
        "Mağazanızı bağlayın", "Mockup'larınızı ekleyin", "Tasarımınızı bırakın")
    assert (en["setup.shop"], en["setup.mockups"], en["setup.designs"]) == (
        "Connect your shop", "Add your mockups", "Drop your design")
    for lang in (tr, en):
        for step in ("account", "shop", "mockups", "designs"):
            assert lang[f"setup.{step}_sub"] != lang[f"setup.{step}"], step


def test_the_feed_is_a_history_and_reminders_only_fill_it():
    js = _read("js/pages/panel.js").replace("\r\n", "\n")
    notes = js.index('for (const it of items) if (it.type === "notification") rows.push(noteRow(it));')
    fill = js.index("if (rows.length < FEED_ROWS) rows.push(...reminderRows().slice(0, FEED_ROWS - rows.length));")
    assert notes < fill
    reminders = js.split("function reminderRows()")[1].split("\n    }\n")[0]
    assert reminders.count("time: relative(") == 3  # no reminder row without a time


def test_the_mini_line_is_a_smooth_rise():
    js = _read("js/pages/panel.js")
    assert 'const LINE_SHAPE = "M2 40 C 14 38, 20 34, 30 30 S 48 22, 58 16 S 74 6, 84 4";' in js
    assert "function monotonePath(pts)" in js and "Catmull" not in js


def test_the_panel_texts_are_the_videos():
    panel = json.loads(_read("i18n/panel.json"))
    assert list(panel["tr"]) == list(panel["en"])
    tr = panel["tr"]
    assert tr["subtitle"] == "Hoş geldiniz"
    assert tr["kpi.drafts"] == "Bu ay oluşturulan taslak"
    assert tr["kpi.speed"] == "Ortalama hazırlanma süresi"
    assert tr["kpi.saved"] == "Tahmini kazanılan zaman"
    assert tr["kpi.seo"] == "SEO ortalaması"
    assert tr["recent.title"] == "Son işlemler" and tr["recent.all"] == "Tümünü gör"
    assert tr["upload.drop"] == "Tasarımlarınızı sürükleyip bırakın"
    assert tr["sync.done"] == "Mağaza senkronize · {time}"
    common = json.loads(_read("i18n/common.json"))
    assert common["tr"]["time.yesterday"] == "Dün" and common["en"]["time.yesterday"] == "Yesterday"
