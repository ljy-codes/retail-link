"""Build a self-contained Intel Mac app; publish only its DMG and HTML manual."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

DESKTOP = Path(__file__).resolve().parent
DEV = DESKTOP.parent
ROOT = DEV.parent
BUILD = DESKTOP / "build"
RELEASE = json.loads((DESKTOP / "release.json").read_text())
VERSION = RELEASE["macVersion"]
APP = BUILD / "零售通.app"


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def clean_copy(source, target):
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def runtime(source, destination):
    """Keep the portable interpreter, stdlib and the two actual dependencies."""
    standard = source / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    version = standard.name
    destination.mkdir(parents=True)
    (destination / "bin").mkdir()
    executable = (source / "bin/python3").resolve()
    shutil.copy2(executable, destination / "bin/python3")
    excluded = {"site-packages", "__pycache__", "ensurepip", "idlelib", "tkinter", "turtledemo", "venv", "config-3.12-darwin"}
    shutil.copytree(standard, destination / "lib" / version,
        ignore=lambda path, names: [name for name in names if name in excluded or name.endswith(".pyc")])
    packages = destination / "lib" / version / "site-packages"
    packages.mkdir()
    for name in ("openpyxl", "et_xmlfile"):
        clean_copy(standard / "site-packages" / name, packages / name)
        for metadata in (standard / "site-packages").glob(name + "-*.dist-info"):
            clean_copy(metadata, packages / metadata.name)
    licenses = DESKTOP / "licenses"
    if licenses.exists():
        clean_copy(licenses, destination / "licenses")
    run(destination / "bin/python3", "-I", "-B", "-c",
        "import openpyxl,et_xmlfile,sqlite3,ssl,hashlib,zlib;print('内置运行环境自检通过')",
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "PYTHONNOUSERSITE": "1"})


def icon(resources):
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(image)
    # Customer-selected red identity with a white operating trend line.
    draw.rounded_rectangle((60, 60, 964, 964), radius=202, fill="#F8E7E5")
    draw.rounded_rectangle((125, 125, 899, 899), radius=165, fill="#BE3835")
    draw.line([(254, 684), (254, 346)], fill="#E8A5A1", width=32)
    draw.line([(254, 684), (779, 684)], fill="#E8A5A1", width=32)
    points = [(285, 596), (423, 491), (553, 548), (723, 355)]
    draw.line(points, fill="#FFFFFF", width=70, joint="curve")
    for x, y in points:
        draw.ellipse((x - 34, y - 34, x + 34, y + 34), fill="#FFFFFF")
    draw.ellipse((672, 304, 774, 406), fill="#FFFFFF")
    (resources / "AppIcon.png").parent.mkdir(parents=True, exist_ok=True)
    image.save(resources / "AppIcon.png")
    image.save(resources / "RetailLink.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icons = BUILD / "RetailLink.iconset"
    icons.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
            image.resize((size * scale, size * scale), Image.Resampling.LANCZOS).save(icons / name)
    run("iconutil", "-c", "icns", icons, "-o", resources / "RetailLink.icns")


def build(python_source):
    assert int(VERSION.split(".")[-1]) % 2 == 1
    windows_version = int(RELEASE["windowsVersion"].split(".")[-1])
    assert windows_version > int(VERSION.split(".")[-1])
    assert windows_version % 2 == 0
    BUILD.mkdir(parents=True, exist_ok=True)
    if APP.exists():
        shutil.rmtree(APP)
    contents = APP / "Contents"
    resources = contents / "Resources"
    executable = contents / "MacOS" / "RetailLink"
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    info = plistlib.loads((DESKTOP / "macos/Info.plist").read_bytes())
    info["CFBundleShortVersionString"] = VERSION
    info["CFBundleVersion"] = VERSION.split(".")[-1]
    info["CFBundleIconFile"] = "RetailLink.icns"
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    run("swiftc", "-O", "-target", "x86_64-apple-macosx13.0", "-framework", "Cocoa", "-framework", "WebKit",
        DESKTOP / "macos/Main.swift", DESKTOP / "macos/LifecycleSupport.swift", "-o", executable)
    app_source = resources / "app"
    app_source.mkdir()
    shutil.copy2(DEV / "server.py", app_source / "server.py")
    for name in ("backend", "web"):
        clean_copy(DEV / name, app_source / name)
    metadata = {"name": "零售通", "version": VERSION, "platform": "mac", "featureLevel": RELEASE["featureLevel"],
                "architecture": "x86_64", "pairedWindowsVersion": RELEASE["windowsVersion"]}
    (app_source / "版本.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    (app_source / "功能清单.json").write_text(json.dumps(RELEASE["features"], ensure_ascii=False, indent=2))
    runtime(python_source, resources / "runtime")
    shutil.copy2(DESKTOP / "使用说明.html", resources / "使用说明.html")
    icon(resources)
    # Local ad-hoc signature: a distributable Developer ID/notarization is a later release step.
    run("codesign", "--force", "--sign", "-", resources / "runtime/bin/python3")
    run("codesign", "--force", "--deep", "--sign", "-", APP)
    run("codesign", "--verify", "--deep", "--strict", APP)
    return APP


def publish():
    staging = BUILD / "交付暂存"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    image_source = BUILD / "DMG内容"
    if image_source.exists():
        shutil.rmtree(image_source)
    image_source.mkdir()
    run("ditto", APP, image_source / APP.name)
    (image_source / "Applications").symlink_to("/Applications", target_is_directory=True)
    dmg = staging / f"零售通-v{VERSION}-mac-Intel.dmg"
    run("hdiutil", "create", "-quiet", "-volname", f"零售通 {VERSION}", "-srcfolder", image_source,
        "-format", "UDZO", "-ov", dmg)
    run("hdiutil", "verify", dmg, stdout=subprocess.DEVNULL)
    shutil.copy2(DESKTOP / "使用说明.html", staging / f"零售通-v{VERSION}-使用说明.html")
    assert len(list(staging.iterdir())) == 2
    dest = ROOT / "产品交付"
    previous = BUILD / "上次交付临时"
    if previous.exists():
        shutil.rmtree(previous)
    if dest.exists():
        dest.replace(previous)
    try:
        staging.replace(dest)
    except Exception:
        if previous.exists():
            previous.replace(dest)
        raise
    if previous.exists():
        shutil.rmtree(previous)
    for path in dest.iterdir():
        print(path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-runtime", type=Path, default=Path(sys.base_prefix))
    parser.add_argument("--publish", action="store_true", help="验证完成后只交付DMG与HTML两个文件")
    args = parser.parse_args()
    print(build(args.python_runtime))
    if args.publish:
        publish()
