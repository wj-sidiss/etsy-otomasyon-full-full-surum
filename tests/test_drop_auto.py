import io
import json
import os
import subprocess
import sys
import threading

import pytest
from PIL import Image
from typer.testing import CliRunner

from etsyotomasyon.cli import app
from etsyotomasyon.drop import automation, pipeline
from etsyotomasyon.drop.template import Template
from etsyotomasyon.drop.workspace import Workspace
from etsyotomasyon.errors import ValidationError


@pytest.fixture
def studio(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    folder = ws.products / "mountain sunset shirt"
    folder.mkdir()
    for name in ("10-detail.png", "2-back.png", "1-front.png"):
        Image.new("RGBA", (20, 20), (20, 30, 40, 100)).save(folder / name)
    template = Template(1, fields={"taxonomy_id": 1, "price": 20, "quantity": 5,
                                    "who_made": "i_did", "when_made": "made_to_order",
                                    "type": "physical"}, description="Cotton shirt.")
    ws.write_template(template.to_dict())
    return ws, template


class Client:
    def __init__(self, ws, fail=None):
        self.ws, self.fail = ws, fail
        self.creates = 0
        self.images = []

    def shop_id(self):
        return 123

    def listing_inventory(self, listing_id):
        # A template without options: one product, no properties.
        return {"products": [{"property_values": [], "offerings": [
            {"price": {"amount": 2000, "divisor": 100}, "quantity": 5, "is_enabled": True}]}]}

    def search_active_listings(self, **kwargs):
        return iter([])

    def create_draft_listing(self, fields):
        self.creates += 1
        state = json.loads((self.ws.root / "upload-history.json").read_text())
        assert state["123"]["mountain sunset shirt"]["status"] == "pending"
        if self.fail == "create":
            raise OSError("response lost")
        return {"listing_id": 900}

    def upload_listing_image(self, listing_id, image, *, rank):
        state = json.loads((self.ws.root / "upload-history.json").read_text())
        assert state["123"]["mountain sunset shirt"]["listing_id"] == 900
        if self.fail == "image":
            raise OSError("upload interrupted")
        self.images.append((image.name, rank))
        return {}


def test_folder_is_one_listing_and_ready_pngs_are_not_composited(studio):
    ws, template = studio
    report = pipeline.run(ws, template)
    assert len(report.ready) == 1
    row = report.ready[0]
    assert row.seed.text == "mountain sunset shirt"
    assert [p.name for p in row.images] == ["1-front.png", "2-back.png", "10-detail.png"]
    assert all(p.parent == row.source for p in row.images)


def test_auto_uploads_all_images_and_second_run_does_not_duplicate(studio):
    ws, template = studio
    client = Client(ws)
    first = automation.run(ws, template, client=client)
    assert first.uploaded.created == 1
    assert client.images == [("1-front.png", 1), ("2-back.png", 2), ("10-detail.png", 3)]
    second = automation.run(ws, template, client=client)
    assert client.creates == 1
    assert second.already_done == ["mountain sunset shirt"]


@pytest.mark.parametrize("failure", ["create", "image"])
def test_uncertain_or_partial_upload_is_not_recreated(studio, failure):
    ws, template = studio
    client = Client(ws, failure)
    automation.run(ws, template, client=client)
    second = automation.run(ws, template, client=client)
    assert client.creates == 1
    assert second.needs_review


def test_cli_dry_run_is_offline_and_does_not_mark_uploaded(studio):
    ws, _ = studio
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "validated" in result.output
    assert not (ws.root / "upload-history.json").exists()


def test_corrupt_image_aborts_before_any_upload(studio):
    ws, template = studio
    (ws.products / "mountain sunset shirt" / "2-back.png").write_text("broken")
    client = Client(ws)
    with pytest.raises(ValidationError, match="Invalid image"):
        automation.run(ws, template, client=client)
    assert client.creates == 0


def test_a_ready_photo_etsy_refuses_is_converted_and_the_row_says_so(studio):
    # Ready photos upload unchanged, so a .webp would reach Etsy as a .webp and be
    # refused after the draft existed. It is converted here, and never silently.
    ws, template = studio
    folder = ws.products / "mountain sunset shirt"
    (folder / "1-front.png").unlink()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(folder / "1-front.webp", "WEBP")

    report = pipeline.run(ws, template)
    row = report.ready[0]
    assert [p.name for p in row.images] == ["1-front-webp.jpg", "2-back.png", "10-detail.png"]
    assert any("1-front.webp was converted" in w for w in row.warnings)
    assert (ws.drafts / report.batch / "mountain sunset shirt" / "1-front-webp.jpg").is_file()


def test_a_truncated_jpeg_is_caught_before_the_first_draft(studio):
    # Image.verify() is a no-op for JPEG — only PNG overrides it — so a photo cut short
    # by a half-finished copy passed this gate and failed on upload, after the draft.
    ws, template = studio
    folder = ws.products / "mountain sunset shirt"
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), (1, 2, 3)).save(buffer, "JPEG", quality=95)
    whole = buffer.getvalue()
    (folder / "1-front.png").unlink()
    (folder / "1-front.jpg").write_bytes(whole[: len(whole) // 2])

    client = Client(ws)
    with pytest.raises(ValidationError, match="Invalid image"):
        automation.run(ws, template, client=client)
    assert client.creates == 0


def test_image_overflow_does_not_silently_drop_photos(studio):
    ws, template = studio
    for n in range(18):
        Image.new("RGB", (20, 20)).save(ws.products / "mountain sunset shirt" / f"extra-{n}.jpg")
    client = Client(ws)
    with pytest.raises(ValidationError, match="more than 20"):
        automation.run(ws, template, client=client)
    assert client.creates == 0


def test_lock_and_corrupt_history_block_writes(studio):
    ws, template = studio
    client = Client(ws)
    lock = ws.root / ".auto-upload.lock"
    lock.write_text("another process")
    with pytest.raises(ValidationError, match="Another auto run"):
        automation.run(ws, template, client=client)
    lock.unlink()
    (ws.root / "upload-history.json").write_text("broken")
    with pytest.raises(ValidationError, match="Cannot read"):
        automation.run(ws, template, client=client)
    assert client.creates == 0


def test_invalid_template_aborts_whole_batch(studio):
    ws, template = studio
    template.fields["price"] = -1
    client = Client(ws)
    with pytest.raises(ValidationError, match="Nothing uploaded"):
        automation.run(ws, template, client=client)
    assert client.creates == 0


class DigitalClient(Client):
    """The auto Client, plus the download-file upload and a context manager (the CLI)."""

    def __init__(self, ws, fail=None, product="mountain sunset shirt"):
        super().__init__(ws, fail)
        self.product = product
        self.files = []
        self.fields = []
        self.order = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def _entry(self):
        return json.loads((self.ws.root / "upload-history.json").read_text())["123"][self.product]

    def create_draft_listing(self, fields):
        self.creates += 1
        self.fields.append(dict(fields))
        assert self._entry()["status"] == "pending"
        return {"listing_id": 900}

    def upload_listing_image(self, listing_id, image, *, rank):
        assert self._entry()["listing_id"] == 900
        self.order.append("image")
        self.images.append((image.name, rank))
        return {}

    def upload_listing_file(self, listing_id, path, *, rank):
        assert self._entry()["listing_id"] == 900
        if self.fail == "file":
            raise OSError("upload interrupted")
        self.order.append("file")
        self.files.append((path.name, rank))
        return {"listing_file_id": 1, "listing_id": listing_id, "rank": rank}


def _download_template(ws, template, listing_type="download"):
    template.fields["type"] = listing_type
    template.fields["when_made"] = "2020_2026"  # made to order would need no download
    ws.write_template(template.to_dict())
    return template


def _downloads(ws, **files):
    folder = ws.products / "mountain sunset shirt" / "dosyalar"
    folder.mkdir()
    for name, data in files.items():
        (folder / name.replace("_", ".")).write_bytes(data)
    return folder


def test_a_digital_folder_without_dosyalar_stops_the_batch_before_any_upload(studio):
    ws, template = studio
    template = _download_template(ws, template)
    client = DigitalClient(ws)
    with pytest.raises(ValidationError, match="no 'dosyalar'"):
        automation.run(ws, template, client=client)
    assert client.creates == 0
    with pytest.raises(ValidationError, match="Nothing uploaded"):
        automation.run(ws, template, dry_run=True)


def test_drop_auto_attaches_a_digital_folders_files_after_its_photos(studio):
    ws, template = studio
    template = _download_template(ws, template)
    _downloads(ws, planner_pdf=b"%PDF-1.4", extras_zip=b"PK")
    client = DigitalClient(ws)
    report = automation.run(ws, template, client=client)

    result = report.uploaded.results[0]
    assert result.status == "ok" and result.files_uploaded == 2
    assert "2 download files attached" in result.message
    assert client.fields[0]["type"] == "download"
    assert client.images == [("1-front.png", 1), ("2-back.png", 2), ("10-detail.png", 3)]
    assert client.files == [("extras.zip", 1), ("planner.pdf", 2)]
    assert client.order == ["image"] * 3 + ["file"] * 2
    entry = json.loads((ws.root / "upload-history.json").read_text())["123"][
        "mountain sunset shirt"]
    assert entry["status"] == "ok" and entry["files_uploaded"] == 2
    assert report.prepared.ready[0].files[0].name == "extras.zip"


def test_drop_auto_attaches_a_loose_designs_original_file(studio):
    ws, template = studio
    template = _download_template(ws, template)
    shutil_rmtree(ws.products / "mountain sunset shirt")
    Image.new("RGBA", (40, 40), (200, 60, 40, 255)).save(ws.products / "mountain sunset shirt.png")
    client = DigitalClient(ws, product="mountain sunset shirt.png")
    report = automation.run(ws, template, client=client)
    assert report.uploaded.results[0].status == "ok"
    assert client.files == [("mountain sunset shirt.png", 1)]
    assert "shipping_profile_id" not in client.fields[0]
    # The design is kept as the listing's print file, under its listing id.
    kept = ws.root / "print-files" / "900" / "mountain sunset shirt.png"
    assert kept.read_bytes() == (ws.products / "mountain sunset shirt.png").read_bytes()


def test_a_file_that_fails_after_the_create_is_partial_and_not_recreated(studio):
    ws, template = studio
    template = _download_template(ws, template)
    _downloads(ws, planner_pdf=b"%PDF-1.4")
    client = DigitalClient(ws, "file")
    report = automation.run(ws, template, client=client)
    result = report.uploaded.results[0]
    assert result.status == "partial" and result.files_uploaded == 0
    assert "file 1 of 1 (planner.pdf) failed" in result.message
    entry = json.loads((ws.root / "upload-history.json").read_text())["123"][
        "mountain sunset shirt"]
    assert entry["status"] == "partial" and entry["files_uploaded"] == 0
    assert entry["images_uploaded"] == 3
    again = automation.run(ws, template, client=client)
    assert client.creates == 1 and again.needs_review


def test_a_both_template_keeps_its_shipping_and_attaches_files(studio):
    ws, template = studio
    template.fields["shipping_profile_id"] = 77
    template = _download_template(ws, template, "both")
    _downloads(ws, planner_pdf=b"%PDF-1.4")
    client = DigitalClient(ws)
    automation.run(ws, template, client=client)
    assert client.fields[0]["type"] == "both" and client.fields[0]["shipping_profile_id"] == 77
    assert client.files == [("planner.pdf", 1)]


def test_cli_drop_auto_with_a_digital_template(studio, monkeypatch):
    import etsyotomasyon.cli as cli

    ws, template = studio
    _download_template(ws, template)
    _downloads(ws, planner_pdf=b"%PDF-1.4")
    dry = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "1 file(s)" in dry.output and "1 validated" in dry.output

    client = DigitalClient(ws)
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root)])
    assert result.exit_code == 0, result.output
    assert "Created 1 draft(s)" in result.output
    assert client.files == [("planner.pdf", 1)] and client.fields[0]["type"] == "download"


def test_cli_drop_auto_stops_a_digital_batch_with_a_missing_file(studio, monkeypatch):
    import etsyotomasyon.cli as cli

    ws, template = studio
    _download_template(ws, template)
    client = DigitalClient(ws)
    monkeypatch.setattr(cli, "_client", lambda **_kw: client)
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root)])
    assert result.exit_code != 0 and isinstance(result.exception, ValidationError)
    assert "no 'dosyalar'" in str(result.exception) and client.creates == 0


def shutil_rmtree(path):
    import shutil

    shutil.rmtree(path)


def _history(ws, entries):
    (ws.root / "upload-history.json").write_text(json.dumps({"123": entries}), encoding="utf-8")


def test_a_dry_run_reads_the_history_the_real_run_will_use(studio):
    # A rehearsal that ignored the history would validate products the real run skips
    # and say nothing about a half-uploaded draft needing a look.
    ws, template = studio
    _history(ws, {"mountain sunset shirt": {"status": "partial", "listing_id": 222}})
    report = automation.run(ws, template, dry_run=True)
    assert report.needs_review == ["mountain sunset shirt: partial, listing 222"]
    assert report.prepared.ready == []


def test_a_case_only_rename_is_still_the_same_product(studio):
    ws, template = studio
    _history(ws, {"Mountain Sunset Shirt": {"status": "ok", "listing_id": 900}})
    client = Client(ws)
    report = automation.run(ws, template, client=client)
    assert client.creates == 0
    assert report.already_done == ["Mountain Sunset Shirt"]


def test_each_uploaded_product_carries_its_own_review_csv_line(studio):
    ws, template = studio
    second = ws.products / "ceramic coffee mug"
    second.mkdir()
    Image.new("RGB", (20, 20), (200, 200, 200)).save(second / "01.jpg")

    class TwoProducts(Client):
        def create_draft_listing(self, fields):
            self.creates += 1
            return {"listing_id": 900 + self.creates}

        def upload_listing_image(self, listing_id, image, *, rank):
            return {}

    report = automation.run(ws, template, client=TwoProducts(ws))
    assert [r.row for r in report.uploaded.results] == [2, 3]


def test_a_create_without_a_listing_id_is_flagged_not_crashed(studio):
    ws, template = studio

    class Odd(Client):
        def create_draft_listing(self, fields):
            return "OK"

    report = automation.run(ws, template, client=Odd(ws))
    result = report.uploaded.results[0]
    assert result.status == "error"
    assert "draft may exist" in result.message
    state = json.loads((ws.root / "upload-history.json").read_text())
    assert state["123"]["mountain sunset shirt"]["status"] != "ok"


def test_review_csv_paths_stay_relative_for_ready_photos(studio):
    ws, template = studio
    report = pipeline.run(ws, template)
    text = report.csv_path.read_text(encoding="utf-8-sig")
    assert str(ws.root) not in text and ws.root.as_posix() not in text
    assert "../../2-PRODUCTS/mountain sunset shirt/1-front.png" in text


def test_the_workspace_readme_is_refreshed_when_it_is_out_of_date(tmp_path):
    ws = Workspace(tmp_path / "studio").create()
    readme = ws.root / "README.txt"
    readme.write_text("ETSY STUDIO\n===========\nan older description", encoding="utf-8")
    ws.create()
    assert "drop auto` uploads the drafts straight away" in readme.read_text(encoding="utf-8")


_VARIED = {
    "products": [
        {"product_id": 1, "sku": "", "is_deleted": False,
         "property_values": [
             {"property_id": 513, "property_name": "Material", "scale_id": None,
              "value_ids": [11], "values": ["Cotton"]},
             {"property_id": 514, "property_name": "Size", "scale_id": None,
              "value_ids": [21], "values": ["Large"]}],
         "offerings": [{"offering_id": 9, "is_deleted": False, "is_enabled": True, "quantity": 99,
                        "price": {"amount": 1250, "divisor": 100, "currency_code": "USD"},
                        "readiness_state_id": 7}]},
        {"product_id": 2, "sku": "", "is_deleted": True, "property_values": [], "offerings": []},
    ],
    "price_on_property": [513, 514], "quantity_on_property": [513, 514], "sku_on_property": [],
}


def test_an_inventory_read_becomes_a_writable_body():
    from etsyotomasyon.listings import inventory_for_copy

    body = inventory_for_copy(_VARIED)
    assert len(body["products"]) == 1, "a deleted product must not be recreated"
    product = body["products"][0]
    assert "product_id" not in product
    assert product["offerings"] == [
        {"price": 12.5, "quantity": 99, "is_enabled": True, "readiness_state_id": 7}
    ]
    assert product["property_values"][0] == {
        "property_id": 513, "property_name": "Material", "value_ids": [11], "values": ["Cotton"]
    }
    assert body["price_on_property"] == [513, 514]


def test_drop_auto_copies_the_template_listings_variations(studio):
    ws, template = studio

    class Varied(Client):
        def __init__(self, ws):
            super().__init__(ws)
            self.inventories = []

        def listing_inventory(self, listing_id):
            return _VARIED

        def update_listing_inventory(self, listing_id, inventory):
            self.inventories.append((listing_id, inventory))
            return {}

    client = Varied(ws)
    report = automation.run(ws, template, client=client)
    assert report.uploaded.results[0].status == "ok"
    assert [lid for lid, _ in client.inventories] == [900]
    assert "1 variations" in report.uploaded.results[0].message
    state = json.loads((ws.root / "upload-history.json").read_text())
    assert state["123"]["mountain sunset shirt"]["variations"] == 1


def test_a_variation_failure_leaves_the_draft_partial_not_ok(studio):
    ws, template = studio

    class Refused(Client):
        def listing_inventory(self, listing_id):
            return _VARIED

        def update_listing_inventory(self, listing_id, inventory):
            raise OSError("inventory refused")

    report = automation.run(ws, template, client=Refused(ws))
    result = report.uploaded.results[0]
    assert result.status == "partial"
    assert "variations could not be set" in result.message


# --- history writes on Windows (history-replace-windows) -------------------------------------


@pytest.fixture
def quick_retries(monkeypatch):
    monkeypatch.setattr(automation, "REPLACE_FIRST_PAUSE", 0.001)
    monkeypatch.setattr(automation, "REPLACE_MAX_PAUSE", 0.002)


def test_a_busy_history_file_is_retried_not_given_up(tmp_path, monkeypatch, quick_retries):
    # Windows refuses os.replace while another program has the file open ("Access
    # denied"): the save waits a moment and tries again.
    path = tmp_path / "upload-history.json"
    real = automation.os.replace
    failures = {"n": 3}

    def busy_replace(src, dst):
        if failures["n"]:
            failures["n"] -= 1
            raise PermissionError(13, "Access denied")
        return real(src, dst)

    monkeypatch.setattr(automation.os, "replace", busy_replace)
    automation.save_history(path, {"123": {"a.png": {"status": "pending"}}})
    assert failures["n"] == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"123": {"a.png": {"status": "pending"}}}
    assert not path.with_suffix(".tmp").exists()


def test_a_history_file_that_stays_busy_is_written_in_place(tmp_path, monkeypatch, quick_retries):
    # Never lose a draft's record: when the file cannot be replaced at all, it is
    # overwritten where it is (a program that only reads it still lets that happen).
    path = tmp_path / "upload-history.json"
    path.write_text(json.dumps({"123": {"old.png": {"status": "ok"}}}) + " " * 500,
                    encoding="utf-8")

    def always_busy(src, dst):
        raise PermissionError(13, "Access denied")

    monkeypatch.setattr(automation.os, "replace", always_busy)
    state = {"123": {"old.png": {"status": "ok"},
                     "new.png": {"status": "pending", "listing_id": 1000001}}}
    automation.save_history(path, state)
    assert automation.load_history(path) == state


def test_another_error_still_stops_the_run(tmp_path, monkeypatch):
    def broken(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(automation.os, "replace", broken)
    with pytest.raises(ValidationError, match="Cannot save upload history"):
        automation.save_history(tmp_path / "upload-history.json", {})


def test_saves_survive_a_reader_that_holds_the_file_open(tmp_path):
    # The review's reproduction: another thread reads the file (as the status check
    # and İlanlar do, without any lock) while it is being replaced over and over.
    path = tmp_path / "upload-history.json"
    automation.save_history(path, {"123": {}})
    stop = threading.Event()
    reads = {"n": 0}

    def reader():
        while not stop.is_set():
            try:
                path.read_text(encoding="utf-8")
                reads["n"] += 1
            except OSError:
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for n in range(150):
            state = {"123": {f"design-{i}.png": {"status": "ok"} for i in range(n % 7)}}
            automation.save_history(path, state)
    finally:
        stop.set()
        thread.join(5)
    assert automation.load_history(path) == state
    assert reads["n"] > 0


def test_readers_in_the_app_share_the_writers_lock(tmp_path):
    path = tmp_path / "upload-history.json"
    errors: list = []

    def read_many():
        for _ in range(150):
            try:
                automation.read_history(tmp_path)
                automation.load_history(path)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=read_many) for _ in range(3)]
    for thread in threads:
        thread.start()
    for n in range(150):
        automation.save_history(path, {"123": {"x.png": {"status": "ok", "n": n}}})
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert automation.read_history(tmp_path)["123"]["x.png"]["n"] == 149


def test_a_progress_save_that_fails_does_not_cost_the_draft_its_images(studio, monkeypatch):
    ws, template = studio
    client = Client(ws)
    real = automation.save_history
    calls = {"n": 0}

    def flaky(path, state):
        calls["n"] += 1
        # 1: intent, 2: the draft id, 3-5: after each image (3 and 4 fail), 6: the outcome.
        if calls["n"] in (3, 4):
            raise ValidationError("Cannot save upload history; stopped to avoid duplicates.")
        return real(path, state)

    monkeypatch.setattr(automation, "save_history", flaky)
    report = automation.run(ws, template, client=client)
    assert report.uploaded.results[0].status == "ok"
    assert len(client.images) == 3, "every image still went up"
    state = json.loads((ws.root / "upload-history.json").read_text())
    entry = state["123"]["mountain sunset shirt"]
    assert entry["status"] == "ok" and entry["listing_id"] == 900
    assert entry["images_uploaded"] == 3


# --- the upload lock (lock-removed-midrun) --------------------------------------------------


def test_a_lock_removed_during_the_run_does_not_turn_it_into_an_error(studio):
    ws, template = studio
    lock = ws.root / ".auto-upload.lock"

    class Unlocking(Client):
        def create_draft_listing(self, fields):
            lock.unlink()  # what the workspace README tells a user with a stuck run to do
            return super().create_draft_listing(fields)

    report = automation.run(ws, template, client=Unlocking(ws))
    assert report.uploaded.created == 1
    assert not lock.exists()


def test_a_lock_another_run_took_meanwhile_is_left_to_it(tmp_path):
    with automation.upload_lock(tmp_path):
        lock = automation.lock_path(tmp_path)
        lock.write_text("4242\nother-host\nsomeone-elses-token\n", encoding="utf-8")
    assert lock.read_text(encoding="utf-8").startswith("4242")


def test_the_lock_says_whose_it_is(tmp_path):
    assert automation.lock_info(tmp_path) is None
    with automation.upload_lock(tmp_path):
        info = automation.lock_info(tmp_path)
        assert info["pid"] == os.getpid() and info["alive"] is True and info["stale"] is False
    assert not automation.lock_path(tmp_path).exists()


def test_a_crashed_runs_lock_is_stale(tmp_path):
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                           capture_output=True, text=True, check=True)
    # The old format: the PID alone.
    automation.lock_path(tmp_path).write_text(ended.stdout.strip(), encoding="utf-8")
    info = automation.lock_info(tmp_path)
    assert info["pid"] == int(ended.stdout) and info["alive"] is False and info["stale"] is True


def test_a_lock_from_another_computer_is_never_called_stale(tmp_path):
    automation.lock_path(tmp_path).write_text("999999\nsome-other-pc\ntoken\n", encoding="utf-8")
    info = automation.lock_info(tmp_path)
    assert info["alive"] is None and info["stale"] is False
    automation.lock_path(tmp_path).write_text("another process", encoding="utf-8")
    assert automation.lock_info(tmp_path)["pid"] is None


# --- mockups for `drop run` / `drop auto`: the app's selection and order -----------------------


@pytest.fixture
def three_mockups(studio):
    from etsyotomasyon.drop import catalog

    ws, template = studio
    for name in ("a-tshirt-white.jpg", "b-mug-white.jpg", "c-tote-cream.jpg"):
        Image.new("RGB", (60, 60), (240, 240, 240)).save(ws.mockups / name)
    catalog.arrange(ws, order=["c-tote-cream.jpg", "a-tshirt-white.jpg", "b-mug-white.jpg"],
                    enabled=["c-tote-cream.jpg", "a-tshirt-white.jpg"])
    return ws, template


def _capture(monkeypatch, module, report):
    seen: dict = {}

    def fake(workspace, *args, **kwargs):
        seen.update(kwargs)
        return report(workspace)

    monkeypatch.setattr(module, "run", fake)
    return seen


def _drop_report(ws):
    return pipeline.DropReport(batch="b", out_dir=ws.drafts / "b")


@pytest.mark.parametrize("extra, expected", [
    ([], ["c-tote-cream.jpg", "a-tshirt-white.jpg"]),
    (["--mockups", "1"], ["c-tote-cream.jpg"]),
    (["--mockups", "9"], ["c-tote-cream.jpg", "a-tshirt-white.jpg"]),
])
def test_drop_run_uses_the_saved_mockup_selection_and_order(three_mockups, monkeypatch, extra,
                                                            expected):
    ws, _ = three_mockups
    seen = _capture(monkeypatch, pipeline, _drop_report)
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(ws.root), *extra])
    assert [p.name for p in seen["mockups"]] == expected, result.output
    assert "Mockups:" in result.output and "1 switched off" in result.output


def test_drop_auto_uses_the_saved_mockup_selection_and_order(three_mockups, monkeypatch):
    ws, _ = three_mockups
    seen = _capture(monkeypatch, automation, lambda ws: automation.AutoReport())
    result = CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert [p.name for p in seen["mockups"]] == ["c-tote-cream.jpg", "a-tshirt-white.jpg"]
    seen.clear()
    CliRunner().invoke(app, ["drop", "auto", "--path", str(ws.root), "--dry-run",
                             "--mockups", "1"])
    assert [p.name for p in seen["mockups"]] == ["c-tote-cream.jpg"]


def test_automation_run_defaults_to_the_saved_selection(three_mockups, monkeypatch):
    ws, template = three_mockups
    seen = _capture(monkeypatch, pipeline, _drop_report)
    automation.run(ws, template, dry_run=True)
    assert [p.name for p in seen["mockups"]] == ["c-tote-cream.jpg", "a-tshirt-white.jpg"]


def test_pipeline_composites_onto_the_given_mockups_in_their_order(three_mockups, monkeypatch):
    from etsyotomasyon.drop import catalog, mockup

    ws, template = three_mockups
    art = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    art.putpixel((20, 20), (200, 30, 30, 255))
    art.save(ws.products / "retro-mountain-sunset.png")
    used = []

    def fake_compose(design, template_image, out, *, area=None, **_kw):
        used.append(template_image.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 20)).save(out, "JPEG")
        return out

    monkeypatch.setattr(mockup, "compose", fake_compose)
    report = pipeline.run(ws, template, mockups=catalog.enabled_mockups(ws))
    assert used == ["c-tote-cream.jpg", "a-tshirt-white.jpg"]
    row = next(r for r in report.rows if r.source.name == "retro-mountain-sunset.png")
    assert not any("were not used" in w for w in row.warnings)


# --- what Etsy accepts: titles, pixels, file size ---------------------------------------------


@pytest.mark.parametrize("title, cleaned", [
    ("Retro Mountain Sunset Shirt, Hiking Tee", "Retro Mountain Sunset Shirt, Hiking Tee"),
    ("Salt & Pepper & Co, Kitchen Gift", "Salt & Pepper and Co, Kitchen Gift"),
    ("Mom + Dad + Me: Family: Tee", "Mom + Dad plus Me: Family - Tee"),
    ("\U0001f338 Flower Power, $5 Tee, 100° Hot", "Flower Power, 5 Tee, 100 Hot"),
    ("Café Latte Mug", "Café Latte Mug"),
    ("\U0001f338\U0001f338", ""),
])
def test_titles_are_cleaned_to_what_etsy_accepts(title, cleaned):
    assert pipeline.clean_title(title) == cleaned
    assert pipeline.title_problems(cleaned) == []


def test_title_problems_name_the_rule():
    problems = pipeline.title_problems("Salt & Pepper & Co $")
    assert any("'$'" in p for p in problems) and any("'&' 2 times" in p for p in problems)
    assert pipeline.title_problems("Mom's Mug | Tea & Coffee ™") == []


def test_a_design_with_too_many_pixels_is_skipped_not_crashed(studio):
    ws, template = studio
    Image.new("1", (12001, 50)).save(ws.products / "huge-mountain-poster.png")
    report = pipeline.run(ws, template)
    row = next(r for r in report.rows if r.source.name == "huge-mountain-poster.png")
    assert row.skipped and any("12001x50" in w for w in row.warnings)
    assert pipeline.oversized([ws.products / "huge-mountain-poster.png"])[1] == (12001, 50)


def test_a_photo_over_etsys_limit_is_made_smaller(tmp_path):
    noisy = Image.effect_noise((600, 600), 90).convert("RGB")
    big = tmp_path / "ocean-waves.png"
    noisy.save(big)
    limit = big.stat().st_size // 3
    out = pipeline.fit_for_etsy(big, tmp_path / "out", limit=limit)
    assert out != big and out.suffix == ".jpg" and out.stat().st_size <= limit
    with Image.open(out) as check:
        check.load()
    small = tmp_path / "small.jpg"
    noisy.resize((50, 50)).save(small)
    assert pipeline.fit_for_etsy(small, tmp_path / "out") == small


def test_the_estimate_counts_variations_and_real_image_counts():
    assert pipeline.estimate_requests(8, 8, 7) == 16 + 8 + 56
    # The review's first demo run: 8 creates, 8 inventory updates, 56 images, 16 research
    # pages and the template's inventory.
    assert pipeline.estimate_requests(8, 8, images=56, has_variations=True,
                                      template_inventory=True) == 89
    assert pipeline.estimate_requests(0, 0, images=0, template_inventory=True) == 0
    # A digital run uploads every product's download files too, one request each.
    assert pipeline.estimate_requests(8, 8, images=56, files=11) == 16 + 8 + 56 + 11


# --- `drop run` for a digital template: the estimate counts the download files ------------------


def test_drop_run_estimate_counts_a_digital_templates_download_files(studio, monkeypatch):
    ws, template = studio
    _download_template(ws, template)
    _downloads(ws, a_pdf=b"%PDF", b_pdf=b"%PDF")
    Image.new("RGBA", (20, 20)).save(ws.products / "retro-sunset.png")
    _capture(monkeypatch, pipeline, _drop_report)
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(ws.root)])
    flat = " ".join(result.output.split())
    # Two products, 1 image each (no mockup + the flat render), 3 downloads: the loose
    # design's own file and the folder's two.
    expected = pipeline.estimate_requests(2, 2, 1, files=3)
    assert "3 download file(s)" in flat, result.output
    assert f"roughly {expected} requests" in flat
    assert expected == pipeline.estimate_requests(2, 2, 1) + 3


def test_drop_run_estimate_of_a_physical_template_names_no_files(studio, monkeypatch):
    ws, _ = studio
    _capture(monkeypatch, pipeline, _drop_report)
    result = CliRunner().invoke(app, ["drop", "run", "--path", str(ws.root)])
    assert "download file" not in result.output
    assert f"roughly {pipeline.estimate_requests(1, 1, 1)} requests" in " ".join(
        result.output.split())


def test_a_row_result_names_its_download_files(capsys):
    from etsyotomasyon import cli
    from etsyotomasyon.listings import RowResult

    cli._print_row_result(RowResult(row=2, action="create", listing_id=900, title="Planner",
                                    images_uploaded=2, files_uploaded=3))
    cli._print_row_result(RowResult(row=3, action="create", listing_id=901, title="Mug",
                                    images_uploaded=1))
    out = " ".join(capsys.readouterr().out.split())
    assert "2 image(s), 3 download file(s)" in out
    assert out.count("download file") == 1


# --- a product folder with only its downloads, in `drop run` / `drop auto` -----------------------


def test_a_folder_without_photos_is_skipped_with_its_reason_and_not_researched(studio):
    ws, template = studio
    (ws.products / "boho planner" / "dosyalar").mkdir(parents=True)
    (ws.products / "boho planner" / "dosyalar" / "planner.pdf").write_bytes(b"%PDF")

    class Recorder(Client):
        def __init__(self, ws):
            super().__init__(ws)
            self.searches = []

        def search_active_listings(self, **kwargs):
            self.searches.append(kwargs["keywords"])
            return iter([])

    client = Recorder(ws)
    report = pipeline.run(ws, template, client=client, use_cache=False)
    skipped = {row.source.name: row for row in report.skipped}
    assert list(skipped) == ["boho planner"]
    assert "has no photos" in skipped["boho planner"].warnings[0]
    assert [row.source.name for row in report.ready] == ["mountain sunset shirt"]
    assert client.searches == ["mountain sunset shirt"]  # already names the shirt
    with pytest.raises(ValidationError, match="has no photos"):
        automation.run(ws, template, dry_run=True)


def test_the_search_names_the_templates_product(studio):
    ws, template = studio
    Image.new("RGBA", (20, 20)).save(ws.products / "retro-sunset.png")
    searched = []

    class Recorder(Client):
        def search_active_listings(self, **kwargs):
            searched.append(kwargs["keywords"])
            return iter([])

    pipeline.run(ws, template, client=Recorder(ws), use_cache=False)
    # "Cotton shirt." is the template: the loose design is searched as a shirt.
    assert sorted(searched) == ["mountain sunset shirt", "retro sunset shirt"]


def test_research_cache_keys_are_the_search_as_the_seo_page_writes_them():
    from etsyotomasyon.drop import cache
    from etsyotomasyon.seo import MarketReport

    report = MarketReport(keyword="dog dad paw print shirt", sampled=3, tags=[], phrases=[],
                          price_min=None, price_median=None, price_max=None, currency="",
                          median_favorers=None, top_listings=[])
    cache.store("dog dad paw print shirt|200", report.__dict__)
    found, cached = pipeline._research_concept(None, "Dog  Dad Paw Print Shirt", sample=200,
                                               use_cache=True)
    assert cached and found.keyword == "dog dad paw print shirt"


def test_the_history_records_each_drafts_tags_and_the_next_batch_avoids_them(studio, monkeypatch):
    ws, template = studio
    client = Client(ws)
    first = automation.run(ws, template, client=client)
    entry = json.loads((ws.root / "upload-history.json").read_text())["123"]["mountain sunset shirt"]
    assert entry["status"] == "ok" and entry["tags"] == first.prepared.ready[0].tags != []

    second = ws.products / "mountain sunset shirt 2"
    second.mkdir()
    Image.new("RGBA", (20, 20), (20, 30, 40, 100)).save(second / "1-front.png")
    seen = []
    real = pipeline.run

    def watching(*args, **kwargs):
        seen.append(kwargs.get("avoid_tags"))
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run", watching)
    automation.run(ws, template, dry_run=True)  # reads the same history, sends nothing
    assert seen == [[entry["tags"]]]
