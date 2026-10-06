#!/usr/bin/env python3
"""Build a local Apple Silicon Portal app using the owner's Steam installation."""

import argparse
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

ENGINE_URL = "https://github.com/nillerusr/source-engine.git"
ENGINE_COMMIT = "ed8209cc35c61fbd8ddff8480962a01c981eef2f"
TESTED_MANIFEST = "3566636281151658894"
DEFAULT_GAME = Path.home() / "Library/Application Support/Steam/steamapps/common/Portal"
DEFAULT_APP = Path.home() / "Applications/Portal Native.app"
LINK_MARKER = "# portal-macos-installer: Steam launcher"
BACKUP_NAME = "hl2_osx.portal-macos-installer-backup"


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def output(args):
    return subprocess.check_output([str(a) for a in args], text=True).strip()


def validate_game(game):
    manifest = game.parent.parent / "appmanifest_400.acf"
    if not manifest.is_file():
        raise RuntimeError(f"Portal's Steam manifest is missing: {manifest}")
    text = manifest.read_text()
    if not re.search(r'"StateFlags"\s+"4"', text):
        raise RuntimeError("Finish updating Portal and close the game before installing.")
    depot = re.search(r'"401"\s*\{([^}]+)\}', text)
    if not depot or not re.search(
        rf'"manifest"\s+"{TESTED_MANIFEST}"', depot.group(1)
    ):
        raise RuntimeError(
            "Portal's content version is not the tested version. In Steam, open "
            "Portal > Properties > Betas and select 'beta - SteamPipe Beta'. "
            "Wait for the download to finish, then try again. "
            "The current public branch can produce broken shaders or a black screen."
        )
    for name in ("portal/gameinfo.txt", "portal/portal_pak_dir.vpk", "hl2/hl2_misc_dir.vpk"):
        if not (game / name).is_file():
            raise RuntimeError(f"Missing game content: {game / name}")


def preflight(game):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Run this installer natively on an Apple Silicon Mac (not under Rosetta).")
    validate_game(game)
    for command in ("brew", "git", "pkg-config", "xcrun", "otool", "install_name_tool", "codesign", "lipo"):
        if not shutil.which(command):
            raise RuntimeError(f"Missing {command}. See the README prerequisites.")
    sdk = output(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
    brew = Path(output(["brew", "--prefix"]))
    if brew != Path("/opt/homebrew"):
        raise RuntimeError("Use native Apple Silicon Homebrew at /opt/homebrew.")
    run(["pkg-config", "--exists", "sdl2", "freetype2", "fontconfig", "libjpeg", "libpng", "libcurl", "zlib"])
    print("Preflight passed: native Apple Silicon tools and tested Portal content.", flush=True)
    return sdk, brew


def build_engine(work, sdk, jobs):
    source = work / "source-engine"
    install = work / "native-install"
    if not source.exists():
        source.mkdir(parents=True)
        run(["git", "init", source])
        run(["git", "-C", source, "remote", "add", "origin", ENGINE_URL])
        run(["git", "-C", source, "fetch", "--depth", "1", "origin", ENGINE_COMMIT])
        run(["git", "-C", source, "checkout", "--detach", "FETCH_HEAD"])
    if output(["git", "-C", source, "rev-parse", "HEAD"]) != ENGINE_COMMIT:
        raise RuntimeError("The build folder contains a different engine revision. Choose a new --work-dir.")
    if output(["git", "-C", source, "status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError("The engine checkout has changes. Choose a new --work-dir.")
    run(["git", "-C", source, "submodule", "update", "--init", "--recursive", "--depth", "1"])
    env = os.environ.copy()
    sdk_arg = shlex.quote(sdk)
    env.update({
        "SDKROOT": sdk,
        "CXXFLAGS": f"-arch arm64 -std=c++11 -isysroot {sdk_arg} -include alloca.h -Wno-null-dereference",
        "CFLAGS": f"-arch arm64 -isysroot {sdk_arg} -include alloca.h",
        "LDFLAGS": f"-arch arm64 {shlex.quote('-L' + sdk + '/usr/lib')} {shlex.quote('-Wl,-syslibroot,' + sdk)}",
    })
    run([sys.executable, "waf", "configure", "-T", "release", "--prefix=", "--build-games=portal", "--disable-warns"], cwd=source, env=env)
    run([sys.executable, "waf", "build", f"-j{jobs}"], cwd=source, env=env)
    run([sys.executable, "waf", "install", f"--destdir={install}"], cwd=source, env=env)
    return source, install


def dependencies(binary):
    refs = output(["otool", "-L", binary]).splitlines()[1:]
    refs = [line.strip().split(" (compatibility", 1)[0] for line in refs]
    # A dylib's first entry is its own install name, not a dependency.
    return refs[1:] if binary.suffix == ".dylib" else refs


def bundle_libraries(resources, source, brew):
    bin_dir = resources / "bin"
    pending = [resources / "hl2_osx"] + list(bin_dir.glob("*.dylib")) + list((resources / "portal/bin").glob("*.dylib"))
    seen = set()
    while pending:
        binary = pending.pop(0)
        if binary in seen:
            continue
        seen.add(binary)
        binary.chmod(0o755)
        # Detect this after SDL2 is copied while traversing engine dependencies.
        # SDL2-compat dlopens SDL3, so otool cannot discover it for us.
        if binary.name.startswith("libSDL2") and b"SDL2COMPAT" in binary.read_bytes():
            sdl3 = brew / "opt/sdl3/lib/libSDL3.dylib"
            dest = bin_dir / "libSDL3.dylib"
            if not sdl3.exists():
                raise RuntimeError("SDL2-compat requires SDL3. Run: brew install sdl3")
            if not dest.exists():
                shutil.copy2(sdl3, dest)
                pending.append(dest)
        for ref in dependencies(binary):
            path = Path(ref)
            if path.is_absolute() and (path.is_relative_to(brew) or path.is_relative_to(source)):
                dest = bin_dir / path.name
                if not dest.exists():
                    shutil.copy2(path, dest)
                    pending.append(dest)
                relative = os.path.relpath(dest, binary.parent)
                run(["install_name_tool", "-change", ref, "@loader_path/" + relative, binary], capture_output=True)
        if binary.suffix == ".dylib":
            run(["install_name_tool", "-id", "@rpath/" + binary.name, binary], capture_output=True)
        run(["codesign", "--force", "--sign", "-", binary], capture_output=True)
    for binary in seen:
        if "arm64" not in output(["lipo", "-archs", binary]).split():
            raise RuntimeError(f"A library is not ARM64: {binary}")
        for ref in dependencies(binary):
            if ref.startswith("/") and not ref.startswith(("/usr/lib/", "/System/Library/")):
                raise RuntimeError(f"Unbundled dependency in {binary}: {ref}")
        run(["codesign", "--verify", binary], capture_output=True)
    print(f"Verified {len(seen)} ARM64 binaries and their local signatures.", flush=True)


def package_app(game, app, work, source, install, brew):
    staging = work / "Portal Native.app"
    if staging.exists():
        raise RuntimeError(f"A previous staging app exists: {staging}. Move it aside or use a new --work-dir.")
    contents = staging / "Contents"
    contents.mkdir(parents=True)
    resources = contents / "Resources"
    # APFS clone when possible; use an ordinary copy on other filesystems.
    copied = subprocess.run(["cp", "-cR", str(game), str(resources)], capture_output=True)
    if copied.returncode:
        if resources.exists():
            shutil.rmtree(resources)
        shutil.copytree(game, resources)
    for folder in ("bin", "portal/bin"):
        target = resources / folder
        if target.is_symlink():
            raise RuntimeError(f"Unexpected symlink in game binaries: {target}")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(install / folder, target)
    shutil.copy2(install / "hl2_launcher", resources / "hl2_osx")
    bundle_libraries(resources, source, brew)
    shutil.copy2(resources / "portal/resource/game.icns", resources / "icon.icns")
    notices = resources / "Engine Notices"
    notices.mkdir()
    for name in ("LICENSE", "thirdpartylegalnotices.txt"):
        shutil.copy2(source / name, notices / name)
    (notices / "BUILD.txt").write_text(f"Engine: {ENGINE_URL}\nCommit: {ENGINE_COMMIT}\nPortal depot 401: {TESTED_MANIFEST}\n")
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleExecutable": "portal",
            "CFBundleIdentifier": "local.portal.apple-silicon",
            "CFBundleName": "Portal Native",
            "CFBundleIconFile": "icon.icns",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": platform.mac_ver()[0],
            "NSHighResolutionCapable": True,
        }, stream)
    launcher = contents / "MacOS/portal"
    launcher.parent.mkdir()
    launcher.write_text('''#!/bin/bash
set -e
PORTAL_RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"
cd "$PORTAL_RESOURCES"
unset DYLD_INSERT_LIBRARIES STEAM_DYLD_INSERT_LIBRARIES
export DYLD_LIBRARY_PATH="$PORTAL_RESOURCES/bin"
exec ./hl2_osx -game portal -novid "$@"
''')
    launcher.chmod(0o755)
    app.parent.mkdir(parents=True, exist_ok=True)
    if app.exists() or app.is_symlink():
        raise RuntimeError(f"Refusing to replace an existing app: {app}. The new app is at {staging}.")
    shutil.move(str(staging), str(app))


def link_steam(game, app):
    launcher = game / "hl2_osx"
    backup = game / BACKUP_NAME
    target = app / "Contents/MacOS/portal"
    if launcher.is_symlink() or backup.is_symlink():
        raise RuntimeError("The Steam launcher or backup is a symlink. Nothing was changed.")
    if not target.is_file() or not os.access(target, os.X_OK):
        raise RuntimeError(f"Build the native app first: {app}")
    existing = launcher.read_bytes()
    if LINK_MARKER.encode() in existing and not backup.is_file():
        raise RuntimeError("The linked launcher has no backup. Verify Portal's files in Steam before linking again.")
    if LINK_MARKER.encode() not in existing:
        if backup.exists():
            raise RuntimeError(f"A launcher backup already exists: {backup}. Restore it before linking again.")
        shutil.copy2(launcher, backup)
    launcher.write_text(f"#!/bin/bash\n{LINK_MARKER}\nexec {shlex.quote(str(target))} \"$@\"\n")
    launcher.chmod(0o755)
    print("Steam's Play button now launches the native app.", flush=True)


def unlink_steam(game):
    launcher = game / "hl2_osx"
    backup = game / BACKUP_NAME
    if launcher.is_symlink() or backup.is_symlink():
        raise RuntimeError("The Steam launcher or backup is a symlink. Nothing was changed.")
    if not backup.is_file():
        raise RuntimeError("No launcher backup was found. Nothing was changed.")
    if not launcher.is_file() or LINK_MARKER.encode() not in launcher.read_bytes():
        raise RuntimeError("The launcher was changed outside this installer. Nothing was overwritten.")
    os.replace(backup, launcher)
    print("Restored the original Steam launcher.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="check prerequisites without building or changing files")
    action.add_argument("--link-steam", action="store_true", help="connect an already built app to Steam's Play button")
    action.add_argument("--unlink-steam", action="store_true", help="restore the Steam launcher saved by --link-steam")
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME, help="Portal folder inside a Steam library")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP, help="destination .app (existing apps are never replaced)")
    parser.add_argument("--work-dir", type=Path, default=Path(__file__).resolve().parent / ".build")
    parser.add_argument("--jobs", type=int, default=4, help="parallel compiler jobs (default: 4)")
    args = parser.parse_args()
    # Do not resolve the destination: an existing symlink must never be followed and overwritten.
    game = args.game_dir.expanduser().resolve()
    app = args.app.expanduser().absolute()
    work = args.work_dir.expanduser().resolve()
    try:
        if args.unlink_steam:
            unlink_steam(game)
            return
        if args.link_steam:
            validate_game(game)
            link_steam(game, app)
            return
        if args.jobs < 1:
            raise RuntimeError("--jobs must be positive.")
        sdk, brew = preflight(game)
        if args.check:
            return
        if app.suffix != ".app" or app.exists() or app.is_symlink():
            raise RuntimeError("Choose a new .app destination with --app; existing apps are never replaced.")
        staging = work / "Portal Native.app"
        if (work.is_relative_to(game) or app.is_relative_to(game)
                or game.is_relative_to(staging) or app == staging
                or app.is_relative_to(work) or work.is_relative_to(app)):
            raise RuntimeError("Keep the game, build workspace, and destination app in separate locations.")
        work.mkdir(parents=True, exist_ok=True)
        print(f"Building engine {ENGINE_COMMIT}. This can take several minutes.", flush=True)
        source, install = build_engine(work, sdk, args.jobs)
        validate_game(game)
        package_app(game, app, work, source, install, brew)
        print(f"\nReady: {app}\nOpen this app in Finder to play.", flush=True)
        print(f"Your original Steam installation was preserved. Build cache: {work}", flush=True)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            print(detail.strip(), file=sys.stderr)
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
