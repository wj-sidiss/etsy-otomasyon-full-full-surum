"""The desktop folder that is the whole user interface for input.

Explorer is already the best drag-and-drop target that will ever exist on this
machine — multi-select, thumbnails, rename in place, works over OneDrive and Remote
Desktop — and it costs nothing to use. So the input surface is a folder, and the
folder names itself in the order you use it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ValidationError

MOCKUPS_DIR = "1-MOCKUPS"
PRODUCTS_DIR = "2-PRODUCTS"
DRAFTS_DIR = "3-DRAFTS"
ARCHIVE_DIR = "archive"
# The shop's info images, which every draft gets after its own photos (drop.infoimages).
# Made when the first one is added; never a product folder, never a workspace of its own.
INFO_DIR = "info-images"
# The original design of every listing, kept under its Etsy listing id so an order can
# be printed (drop.printfiles). Made when the first one is kept; never a product folder.
PRINT_DIR = "print-files"

# The numbered folders a workspace makes inside itself. None of them is ever a workspace
# of its own, and none of them is ever a product folder (see `root_for`).
SUBFOLDER_NAMES = (MOCKUPS_DIR, PRODUCTS_DIR, DRAFTS_DIR)

# Calibration previews go under 3-DRAFTS, never beside the templates: anything with an
# image extension in 1-MOCKUPS *is* a mockup as far as mockup_files() is concerned, so a
# preview written there would come back as a template to composite designs onto.
CALIBRATION_DIR = "calibration"

# A folder product of a digital template keeps the files a buyer downloads in a
# subfolder of this name (either spelling, any case); its photos stay in the folder
# itself (drop.pipeline.files_folder).
FILES_DIRS = ("dosyalar", "files")

TEMPLATE_FILE = "product.json"
POSITIONS_FILE = "positions.json"
README_FILE = "README.txt"
# The first line of the generated README: a README.txt that starts otherwise is not ours.
README_MARK = "ETSY STUDIO"
# What a workspace keeps beside its folders (automation's history and lock included).
HISTORY_FILE = "upload-history.json"
# The seller's watermark and its settings (drop.watermark).
WATERMARK_FILES = ("watermark.png", "watermark.json")
_WORKSPACE_ENTRIES = frozenset(
    name.casefold() for name in (
        MOCKUPS_DIR, PRODUCTS_DIR, DRAFTS_DIR, ARCHIVE_DIR, INFO_DIR, PRINT_DIR, README_FILE,
        TEMPLATE_FILE, HISTORY_FILE, *WATERMARK_FILES, ".auto-upload.lock", "desktop.ini",
        "thumbs.db", ".ds_store",
    )
)

# Stems ending in one of these are a generator's own preview of the artwork beside it,
# not a product. English and Turkish spellings are both covered.
PREVIEW_SUFFIXES = ("-vitrin", "-preview", "-onizleme", "-thumb", "-mockup")

# Design files Pillow can open without an extra. HEIC is deliberately absent: it
# needs pillow-heif, and a missing-extra message beats a decode traceback.
#
# This is the INPUT set and it stays wide on purpose — it is not what Etsy accepts.
# That is `client.UPLOADABLE_SUFFIXES`, which is only JPG/PNG/GIF. A .tif mockup or
# a .bmp design is perfectly usable here because the compositor writes JPEG either
# way, and narrowing this list would make a file the seller dropped into the folder
# simply not appear — the one failure a drag-and-drop input surface must never have.
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

README_TEXT = """\
ETSY STUDIO
===========

1-MOCKUPS   Put your mockup templates here (photos of a blank shirt, mug, poster).
            You only do this once. If a design lands in the wrong place on one,
            run: etsyotomasyon drop calibrate --preview

2-PRODUCTS  Put the designs you want listed here. This is the folder you use every
            time. One loose design = one listing. For ready photos, create one
            folder per product and put its numbered images inside (01, 02, ...).
            Name the folder after the product. Run: etsyotomasyon drop auto

            Digital products (a template listing of type download or both):
            a loose design is delivered as the file itself. A product folder
            delivers the files in its dosyalar (or files) subfolder, at most 5,
            each up to 20 MB; its photos stay in the product folder itself.
            Folders inside dosyalar are not sent: zip each into one file.
            A product folder with only a dosyalar folder and no photos stops
            at the check: every listing needs at least one photo.

3-DRAFTS    What comes out: composited images and review.csv.
            `etsyotomasyon drop run` stops here so you can check review.csv first.
            `etsyotomasyon drop auto` uploads the drafts straight away, in one step.

archive/    Optional manual archive. Files are not moved automatically.

info-images/  Optional: the pictures every listing ends with (materials, sizes,
            how to install, how to measure). Pick them in the app on the Template
            listing page, or put JPG/PNG files here. Every draft gets them after
            its own photos, at most 10. Etsy takes 20 pictures per listing, so
            each one leaves room for one mockup less.

print-files/  The print file of every listing, in a folder named after its Etsy
            listing id (print-files/4567957350/). Every draft keeps a copy of the
            design it was made from here; add the file of any other listing in the
            app (Listings, open the listing) or copy it into its folder yourself.
            Without it an order of that listing cannot be printed.

upload-history.json (next to these folders) records every automatic upload.
Keep it: it is what stops `drop auto` from uploading a product twice.

watermark.png and watermark.json (optional; set them on the app's Mockups page or
with `etsyotomasyon drop watermark`): your mark, stamped on copies of the listing photos
of digital products (or of every listing). Never on the files buyers download.

Nothing here is ever published. Listings are created as DRAFTS in your Etsy shop
and stay invisible to buyers until you publish them yourself.

--------------------------------------------------------------------------------

ETSY STUDIO (TR)
================

1-MOCKUPS   Mockup sablonlarini buraya koy (bos tisort, kupa, poster fotograflari).
            Bunu sadece bir kez yaparsin. Tasarim yanlis yere denk geliyorsa
            komut: etsyotomasyon drop calibrate --preview

2-PRODUCTS  Listelemek istedigin tasarimlari buraya koy. Her seferinde
            kullanacagin klasor bu. Hazir mockuplar icin her urune ayri bir klasor
            ac; 01, 02 diye siraladigin resimleri icine koy. Klasore urunun adini
            ver. Komut: etsyotomasyon drop auto. Bir urun klasoru = bir listing.

            Dijital urunler (turu download ya da both olan sablon ilan): tek
            basina bir tasarimda alici dosyanin kendisini indirir. Urun
            klasorunde alici, klasorun icindeki dosyalar (ya da files) alt
            klasorundeki dosyalari indirir: en fazla 5 dosya, her biri en fazla
            20 MB. Fotograflar yine urun klasorunun kendisinde durur. dosyalar
            icindeki klasorler gonderilmez: her birini zip'leyip tek dosya yap. Icinde
            yalnizca dosyalar klasoru olan, fotografi olmayan bir urun klasoru
            kontrolde durur: her ilanin en az bir fotografi olmali.

3-DRAFTS    Cikan sonuc: giydirilmis gorseller ve review.csv.
            `etsyotomasyon drop run` burada durur; once review.csv'ye bakarsin.
            `etsyotomasyon drop auto` ise taslaklari tek adimda hemen yukler.

archive/    Istersen elle arsivleyebilirsin; otomatik tasima yapilmaz.

info-images/  Istege bagli: her ilanin sonuna eklenen gorseller (malzeme, olcu,
            kurulum, olcum). Uygulamada Sablon Ilan sayfasindan sec ya da JPG/PNG
            dosyalarini buraya koy. Her taslak bunlari kendi fotograflarindan sonra
            alir, en fazla 10. Etsy ilan basina 20 gorsel alir; her biri bir
            mockup'lik yer kaplar.

print-files/  Her ilanin baski dosyasi, Etsy ilan numarasi adli bir klasorde
            (print-files/4567957350/). Her taslak, yapildigi tasarimin bir kopyasini
            burada tutar; baska bir ilanin dosyasini uygulamada (Ilanlar, ilani ac)
            ekle ya da klasorune kendin kopyala. Dosyasi olmayan ilanin siparisi
            basilamaz.

upload-history.json (bu klasorlerin yaninda) her otomatik yuklemeyi kaydeder.
Silme: `drop auto`nun ayni urunu iki kez yuklemesini engelleyen bu dosya.

watermark.png ve watermark.json (istege bagli; uygulamanin Mockuplar sayfasindan ya da
`etsyotomasyon drop watermark` ile ayarlanir): filigraniniz. Dijital urunlerin (ya da tum
ilanlarin) ilan fotograflarinin kopyalarina basilir; alicinin indirdigi dosyalara asla.

Hicbir sey yayinlanmaz. Listingler Etsy magazanda TASLAK olarak olusturulur ve sen
kendin yayinlayana kadar alicilar goremez.
"""


@dataclass
class Workspace:
    root: Path

    @property
    def mockups(self) -> Path:
        return self.root / MOCKUPS_DIR

    @property
    def products(self) -> Path:
        return self.root / PRODUCTS_DIR

    @property
    def drafts(self) -> Path:
        return self.root / DRAFTS_DIR

    @property
    def archive(self) -> Path:
        return self.root / ARCHIVE_DIR

    @property
    def calibration(self) -> Path:
        return self.drafts / CALIBRATION_DIR

    @property
    def info_images(self) -> Path:
        return self.root / INFO_DIR

    @property
    def template_path(self) -> Path:
        return self.root / TEMPLATE_FILE

    @property
    def positions_path(self) -> Path:
        return self.mockups / POSITIONS_FILE

    def create(self) -> Workspace:
        for path in (self.mockups, self.products, self.drafts, self.archive):
            path.mkdir(parents=True, exist_ok=True)
        readme = self.root / README_FILE
        # The file is generated, so it is refreshed whenever the text changes; an
        # older copy would keep describing behaviour the tool no longer has. A README the
        # seller (or another program) wrote in that folder is theirs: never replaced.
        try:
            current: str | None = readme.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        except (OSError, UnicodeDecodeError):
            current = "" if readme.exists() else None
        if current is None or (current != README_TEXT and current.startswith(README_MARK)):
            readme.write_text(README_TEXT, encoding="utf-8")
        return self

    def require(self) -> Workspace:
        if not self.root.is_dir():
            raise ValidationError(
                f"No workspace at {self.root}. Create one with: etsyotomasyon drop init"
            )
        missing = [d.name for d in (self.mockups, self.products) if not d.is_dir()]
        if missing:
            raise ValidationError(
                f"{self.root} is missing {', '.join(missing)}. "
                "Re-run `etsyotomasyon drop init` to repair it."
            )
        return self

    # --- contents ---------------------------------------------------------------

    def mockup_files(self) -> list[Path]:
        return _images(self.mockups)

    def product_files(self, *, exclude_suffixes: tuple[str, ...] = PREVIEW_SUFFIXES) -> list[Path]:
        """Designs waiting to be listed, minus the preview renders beside them.

        Print-on-demand generators habitually write a small showcase render next to
        the full-resolution artwork, named from the same stem. Both land in the same
        folder, and listing the preview would publish a downscaled stand-in as though
        it were the product — so the known suffixes are skipped by default, and the
        argument is there for a generator that uses a different one.
        """
        files = _images(self.products)
        return [f for f in files if not any(f.stem.endswith(s) for s in exclude_suffixes)]

    def product_groups(self) -> list[tuple[Path, list[Path]]]:
        """Loose designs retain their old behavior; each folder is one ready product.

        Folder images are already finished mockups, including transparent PNGs.
        Number them 01, 02, ... to choose their listing order.

        A folder that holds a `dosyalar` / `files` subfolder but no photos is listed too,
        with no images, so it is reported (it has nothing to show buyers) instead of
        vanishing. Tell a folder from a loose design by `path.is_dir()`, not by its images.
        """
        groups = [(path, []) for path in self.product_files()]
        # 2-PRODUCTS was itself used as a workspace by an older version: its archive/
        # holds the designs the seller took out of a run there, never a product.
        nested = is_workspace(self.products)
        for folder in sorted(self.products.iterdir(), key=lambda p: p.name.casefold()):
            if _workspace_folder(folder) or (nested and _named(folder, (ARCHIVE_DIR,))):
                # What a workspace chosen as 2-PRODUCTS left behind (2-PRODUCTS\1-MOCKUPS,
                # 2-PRODUCTS\3-DRAFTS, ...): blank mockups and old renders, never a product.
                continue
            if folder.is_dir() and not folder.is_symlink():
                images = _images(folder)
                if images:
                    groups.append((folder, sorted(images, key=_natural_key)))
                elif has_files_folder(folder):
                    groups.append((folder, []))
        return groups

    def read_template(self) -> dict[str, Any]:
        if not self.template_path.exists():
            raise ValidationError(
                "No product template yet. Point at a listing you built by hand:\n"
                "  etsyotomasyon drop template --from-listing <listing_id>\n"
                "If your shop is empty, create one listing properly in Etsy first — "
                "every draft copies its settings."
            )
        try:
            return json.loads(self.template_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{self.template_path} is not valid JSON: {exc}") from exc

    def write_template(self, data: dict[str, Any]) -> None:
        self.template_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


def has_files_folder(folder: Path) -> bool:
    """Whether a product folder has a `dosyalar` / `files` subfolder (any case)."""
    try:
        return any(
            entry.name.casefold() in FILES_DIRS and entry.is_dir() and not entry.is_symlink()
            for entry in folder.iterdir()
        )
    except OSError:
        return False


def _natural_key(path: Path) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path.name.casefold())]


def is_workspace(path: Path) -> bool:
    """A folder that already holds a workspace: its 1-MOCKUPS and 2-PRODUCTS are there."""
    try:
        return (path / MOCKUPS_DIR).is_dir() and (path / PRODUCTS_DIR).is_dir()
    except OSError:
        return False


def _named(path: Path, names: tuple[str, ...]) -> bool:
    folded = path.name.casefold()
    return any(folded == name.casefold() for name in names)


def _workspace_folder(path: Path) -> bool:
    """1-MOCKUPS / 2-PRODUCTS / 3-DRAFTS by name (any case), or a whole workspace."""
    return _named(path, SUBFOLDER_NAMES) or is_workspace(path)


def root_for(path: Path) -> Path:
    """The workspace root to use when `path` is chosen as the workspace.

    Choosing a folder of a workspace as the workspace itself would build a second one
    inside the first: pick "Etsy Studio\\2-PRODUCTS" and its own 1-MOCKUPS, 2-PRODUCTS
    and 3-DRAFTS appear in there, designs are looked for in "2-PRODUCTS\\2-PRODUCTS" and
    the mockups already in "Etsy Studio\\1-MOCKUPS" are not found. So:

    - a folder anywhere inside an existing workspace's own folders (2-PRODUCTS, a product
      folder in it, 3-DRAFTS\\calibration, archive, ...) gives that workspace's root;
      with workspaces nested by an older version, the outermost one;
    - otherwise a folder named 1-MOCKUPS, 2-PRODUCTS or 3-DRAFTS (any case) gives its
      parent, which becomes the workspace - unless the parent is a drive's root;
    - anything else is used as it is. A workspace kept beside those folders ("Etsy
      Studio\\Shop 2") or under a folder that happens to be one (a home folder someone
      ran `drop init` in) is a workspace of its own.

    The name alone only decides when the parent is new or holds nothing but what a
    workspace keeps (its folders, README.txt, product.json, upload-history.json): a fresh
    "Desktop\\2-PRODUCTS" never turns the whole Desktop into the workspace.

    `path` should be absolute. Only the folders on the way up are looked at; nothing is
    created or moved.
    """
    path = Path(os.path.normpath(str(path)))
    outer = enclosing_root(path)
    if outer is not None:
        return outer
    parent = path.parent
    if (_named(path, SUBFOLDER_NAMES) and parent != path and parent != Path(parent.anchor)
            and _only_workspace_entries(parent)):
        return parent
    return path


def enclosing_root(path: Path) -> Path | None:
    """The outermost existing workspace `path` is a folder of (root_for's first rule).

    None when there is none: a folder merely NAMED 2-PRODUCTS is not inside a workspace.
    """
    path = Path(os.path.normpath(str(path)))
    outer: Path | None = None
    child = path
    for parent in path.parents:
        if _named(child, SUBFOLDER_NAMES + (ARCHIVE_DIR, INFO_DIR, PRINT_DIR)) and is_workspace(parent):
            outer = parent  # keep going: the outermost one wins
        child = parent
    return outer


def _only_workspace_entries(folder: Path) -> bool:
    """The folder does not exist yet, or holds only what a workspace keeps."""
    try:
        return all(
            entry.name.casefold() in _WORKSPACE_ENTRIES or entry.name.startswith(".")
            for entry in folder.iterdir()
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False


def adopt_nested(inner: Path, outer: Path) -> list[str]:
    """Carry what a nested workspace holds for the seller over to the one around it.

    v0.2.0 made "Etsy Studio\\2-PRODUCTS" a workspace of its own when it was chosen, and
    sellers then worked in it: its product.json is the template they use, and its
    upload-history.json is the only thing that stops a design already drafted from being
    drafted again. Switching to the outer workspace must not lose either:
    - history: every shop's entries of `inner` are added to `outer`'s, never over an entry
      `outer` already has (keys are names inside 2-PRODUCTS, so they still match once the
      designs are moved);
    - template: `inner`'s product.json is moved to `outer`; an older, different one there
      is kept as product.json.bak.
    Returns what was carried over ("history", "template"); [] when nothing was. Raises
    ValidationError (UploadLocked while a run holds `outer`'s lock) when the history
    cannot be read or saved; nothing is lost then, and `inner` keeps its files.
    """
    from . import automation  # automation imports this module

    moved: list[str] = []
    if _same_folder(inner, outer) or not inner.is_dir() or not outer.is_dir():
        return moved
    inner_history = automation.history_path(inner)
    if inner_history.is_file():
        # The file stays in `inner` as it is: a history is never deleted.
        theirs = automation.load_history(inner_history)
        with automation.upload_lock(outer):
            path = automation.history_path(outer)
            ours = automation.load_history(path)
            added = False
            for shop, entries in theirs.items():
                section = ours.setdefault(shop, {})
                known = {name.casefold() for name in section}
                for name, entry in entries.items():
                    if name.casefold() not in known:
                        section[name] = entry
                        known.add(name.casefold())
                        added = True
            if added:
                automation.save_history(path, ours)
                moved.append("history")
    template = inner / TEMPLATE_FILE
    target = outer / TEMPLATE_FILE
    try:
        if template.is_file() and not (
            target.is_file() and target.read_bytes() == template.read_bytes()
        ):
            if target.is_file():
                backup = outer / f"{TEMPLATE_FILE}.bak"
                number = 1
                while backup.exists():
                    number += 1
                    backup = outer / f"{TEMPLATE_FILE}.bak{number}"
                os.replace(target, backup)
            os.replace(template, target)
            moved.append("template")
    except OSError as exc:
        raise ValidationError(f"Could not move {template} to {outer}: {exc}") from exc
    return moved


def _same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def desktop_dir() -> Path:
    """Desktop if there is one, home otherwise. Never guesses a localised name."""
    desktop = Path.home() / "Desktop"
    return desktop if desktop.is_dir() else Path.home()


def default_root() -> Path:
    """The selected shop's products folder.

    "Etsy Studio" for the first shop and "Etsy Studio - <shop id>" for each further
    one, so two shops never share products, a template or an upload history. The id
    rather than the Etsy shop name, so the folder does not give the shop away in a
    screenshot.
    """
    from ..config import base_home, home_dir

    home = home_dir()
    name = "Etsy Studio" if home == base_home() else f"Etsy Studio - {home.name}"
    return desktop_dir() / name
