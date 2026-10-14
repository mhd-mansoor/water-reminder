#!/usr/bin/env python3
"""
Build the one-click app for the current OS with PyInstaller.

    pip install "PySide6-Essentials>=6.5" "pyinstaller>=6.10" pillow
    python packaging/build.py

Output (in dist/release/):
    Windows  ->  WaterReminder-Windows.zip      (folder with WaterReminder.exe)
    macOS    ->  WaterReminder-macOS.dmg      (contains "Water Reminder.app")
    Linux    ->  WaterReminder-Linux-x86_64

GitHub Actions runs this on all three systems (see .github/workflows/release.yml).
"""
import os
import plistlib
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
REL = DIST / "release"
SYSTEM = platform.system()


def app_module():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    import water_reminder as w
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    return w


def make_icons(w):
    from PIL import Image
    BUILD.mkdir(exist_ok=True)
    png = BUILD / "icon.png"
    w.render_app_icon(1024).save(str(png))
    im = Image.open(png).convert("RGBA")
    im.save(BUILD / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    if SYSTEM == "Darwin":
        iconset = BUILD / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            im.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
            im.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(BUILD / "icon.icns")], check=True)


def pyinstaller(extra):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", "WaterReminder", "--distpath", str(DIST), "--workpath", str(BUILD / "pyi"),
           "--specpath", str(BUILD), "--noupx", "--exclude-module", "tkinter", *extra, str(ROOT / "water_reminder.py")]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def windows_version_file(version):
    import re
    nums = tuple(int(x) for x in (re.findall(r"\d+", version) + ["0", "0", "0", "0"])[:4])
    path = BUILD / "version.txt"
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('CompanyName', 'Water Reminder (open source)'),
          StringStruct('FileDescription', 'Water Reminder'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', 'WaterReminder'),
          StringStruct('LegalCopyright', 'MIT License'),
          StringStruct('OriginalFilename', 'WaterReminder.exe'),
          StringStruct('ProductName', 'Water Reminder'),
          StringStruct('ProductVersion', '{version}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""")
    return path


def smoke_test(exe):
    """Run the packaged app once (--status) to prove it starts on this OS."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    for flag in ("--status", "--selftest"):
        r = subprocess.run([str(exe), flag], env=env, timeout=180)
        if r.returncode != 0:
            sys.exit(f"Smoke test failed: {exe} {flag} exited with {r.returncode}")
        print(f"Smoke test passed: {exe.name} {flag}", flush=True)


def main():
    w = app_module()
    version = w.APP_VERSION
    make_icons(w)
    shutil.rmtree(REL, ignore_errors=True)
    REL.mkdir(parents=True)

    if SYSTEM == "Windows":
        # A folder build (not --onefile): far fewer antivirus false positives, faster start.
        pyinstaller(["--onedir", "--icon", str(BUILD / "icon.ico"),
                     "--version-file", str(windows_version_file(version))])
        smoke_test(DIST / "WaterReminder" / "WaterReminder.exe")
        shutil.make_archive(str(REL / "WaterReminder-Windows"), "zip", root_dir=DIST, base_dir="WaterReminder")

    elif SYSTEM == "Darwin":
        pyinstaller(["--icon", str(BUILD / "icon.icns"), "--osx-bundle-identifier", "com.waterreminder.app"])
        app = DIST / "Water Reminder.app"
        shutil.rmtree(app, ignore_errors=True)
        shutil.move(str(DIST / "WaterReminder.app"), app)
        info = app / "Contents" / "Info.plist"
        with open(info, "rb") as f:
            pl = plistlib.load(f)
        pl.update({
            "CFBundleName": "Water Reminder",
            "CFBundleDisplayName": "Water Reminder",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "LSUIElement": True,              # menu-bar app: no Dock icon
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        })
        with open(info, "wb") as f:
            plistlib.dump(pl, f)
        # re-sign (ad-hoc) after editing Info.plist, otherwise Apple Silicon refuses to run it
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
        smoke_test(app / "Contents" / "MacOS" / "WaterReminder")
        stage = BUILD / "dmg"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        shutil.copytree(app, stage / app.name, symlinks=True)
        os.symlink("/Applications", stage / "Applications")
        for attempt in range(4):  # hdiutil is occasionally "Resource busy" on CI runners
            r = subprocess.run(["hdiutil", "create", "-volname", "Water Reminder", "-srcfolder", str(stage),
                                "-ov", "-format", "UDZO", str(REL / "WaterReminder-macOS.dmg")])
            if r.returncode == 0:
                break
            time.sleep(5)
        else:
            sys.exit("hdiutil failed")

    else:
        pyinstaller(["--onefile"])
        out = REL / f"WaterReminder-Linux-{platform.machine()}"
        shutil.move(str(DIST / "WaterReminder"), out)
        out.chmod(0o755)
        smoke_test(out)

    print("\nBuilt:", *[p.name for p in REL.iterdir()], sep="\n  ")


if __name__ == "__main__":
    main()
