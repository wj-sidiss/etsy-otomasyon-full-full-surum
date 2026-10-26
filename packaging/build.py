"""Build the downloadable desktop app for the platform this runs on.

    pip install -e . "pyinstaller>=6.10"
    python packaging/build.py

Windows produces one self-contained `etsyotomasyon-<version>-windows.exe`; macOS produces
`etsyotomasyon-<version>-macos.zip` (Apple Silicon) or `-macos-intel.zip` (Intel) holding
`etsyotomasyon.app`. Both land in `dist/release/`.
Neither needs Python on the machine it runs on.

Releases are built by .github/workflows/release.yml on GitHub's runners, not on a
developer's machine, so no local path or user name ends up inside a published binary.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "app"
DIST = ROOT / "dist"
RELEASE = DIST / "release"

sys.path.insert(0, str(ROOT))
from etsyotomasyon import __version__  # noqa: E402
from etsyotomasyon.desktop import icon  # noqa: E402

WINDOWS_VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple}, prodvers={tuple}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'etsyotomasyon contributors'),
      StringStruct('FileDescription', 'etsyotomasyon - Etsy seller automation'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'etsyotomasyon'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'etsyotomasyon.exe'),
      StringStruct('ProductName', 'etsyotomasyon'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def _version_tuple() -> tuple[int, int, int, int]:
    parts = [int(p) for p in __version__.split(".")[:3] if p.isdigit()]
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def _stamp_bundle_version(app: Path) -> None:
    """PyInstaller's command line cannot set a bundle version, so every .app would
    say 0.0.0. Write the real one, then re-sign: an edited Info.plist breaks the
    ad-hoc signature, and Gatekeeper calls an unsigned Apple Silicon app damaged.

    LSUIElement: the app has no window of its own — it lives in the browser — so it
    gets no Dock icon that would sit there doing nothing."""
    import plistlib

    info_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleShortVersionString"] = __version__
    info["CFBundleVersion"] = __version__
    info["LSUIElement"] = True
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)


def main() -> int:
    import PyInstaller.__main__

    system = platform.system()
    if system not in ("Windows", "Darwin"):
        print(f"Desktop builds are made for Windows and macOS, not {system}.")
        return 1

    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)
    RELEASE.mkdir(parents=True, exist_ok=True)

    args = [
        str(ROOT / "packaging" / "etsyotomasyon_app.py"),
        "--name", "etsyotomasyon",
        "--noconfirm",
        "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        # Commands are imported lazily and Rich loads its Unicode tables by name;
        # collect both whole rather than trust static analysis to find them.
        "--collect-submodules", "etsyotomasyon",
        "--collect-submodules", "rich",
        # The web UI (etsyotomasyon/web/static: HTML, JS, CSS, strings) is data, not code.
        "--collect-data", "etsyotomasyon",
        # The window is a web page now; Tcl/Tk would only add megabytes.
        "--exclude-module", "tkinter",
    ]

    if system == "Windows":
        ico = BUILD / "etsyotomasyon.ico"
        icon.write_ico(str(ico))
        version_file = BUILD / "version.txt"
        version_file.write_text(
            WINDOWS_VERSION_INFO.format(tuple=_version_tuple(), version=__version__), encoding="utf-8"
        )
        # Windowed, so a double-click opens the browser and nothing else. (A console
        # build with --hide-console would print to terminals natively, but on
        # Windows 11 its console opens in Windows Terminal, which cannot be hidden:
        # pyinstaller/pyinstaller#8022.) Run from a terminal with arguments, the app
        # attaches to that terminal itself — see etsyotomasyon.desktop._attach_console.
        args += [
            "--onefile", "--windowed",
            "--icon", str(ico), "--version-file", str(version_file),
        ]
    else:
        png = BUILD / "etsyotomasyon.png"
        icon.render(512).save(png)
        args += [
            "--windowed", "--icon", str(png),
            "--osx-bundle-identifier", "io.github.wj-sidiss.etsyotomasyon",
        ]

    PyInstaller.__main__.run(args)

    if system == "Windows":
        target = RELEASE / f"etsyotomasyon-{__version__}-windows.exe"
        shutil.copy2(DIST / "etsyotomasyon.exe", target)
    else:
        _stamp_bundle_version(DIST / "etsyotomasyon.app")
        # Apple Silicon keeps the plain name the README links to; an Intel build says so.
        suffix = "macos-intel" if platform.machine() == "x86_64" else "macos"
        target = RELEASE / f"etsyotomasyon-{__version__}-{suffix}.zip"
        target.unlink(missing_ok=True)
        # ditto keeps the bundle's symlinks and permissions; zipfile would not.
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(DIST / "etsyotomasyon.app"), str(target)], check=True
        )
    print(f"Built {target} ({target.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
