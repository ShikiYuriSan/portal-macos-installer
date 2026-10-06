"""Exercise file validation and real launcher backup/restore without game assets."""

from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tempfile
import unittest

import install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portal installer ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "steamapps/common/Portal"
        self.game.mkdir(parents=True)
        self.manifest = self.game.parent.parent / "appmanifest_400.acf"
        self.write_manifest()
        for file in ("portal/gameinfo.txt", "portal/portal_pak_dir.vpk", "hl2/hl2_misc_dir.vpk"):
            target = self.game / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("test fixture only")
        self.launcher = self.game / "hl2_osx"
        self.original = b"original launcher fixture\x00\x01"
        self.launcher.write_bytes(self.original)
        self.launcher.chmod(0o751)
        self.app = self.root / "Portal 'Native' $(literal).app"
        self.native = self.app / "Contents/MacOS/portal"
        self.native.parent.mkdir(parents=True)
        self.args_file = self.root / "received arguments"
        self.native.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" > " + shlex.quote(str(self.args_file)) + "\n")
        self.native.chmod(0o755)

    def write_manifest(self, content=install.TESTED_MANIFEST, state="4"):
        self.manifest.write_text(
            f'"AppState" {{ "StateFlags" "{state}" "InstalledDepots" '
            f'{{ "401" {{ "manifest" "{content}" }} '
            f'"999" {{ "manifest" "{install.TESTED_MANIFEST}" }} }} }}'
        )

    def test_valid_content_passes_without_writing(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        install.validate_game(self.game)
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_matching_manifest_in_wrong_depot_is_rejected(self):
        self.write_manifest(content="wrong-public-manifest")
        with self.assertRaisesRegex(RuntimeError, "SteamPipe Beta"):
            install.validate_game(self.game)

    def test_updating_game_is_rejected(self):
        self.write_manifest(state="6")
        with self.assertRaisesRegex(RuntimeError, "Finish updating"):
            install.validate_game(self.game)

    def test_missing_game_content_is_rejected(self):
        (self.game / "hl2/hl2_misc_dir.vpk").unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing game content"):
            install.validate_game(self.game)

    def test_link_launch_relink_restore_preserves_original_and_literal_arguments(self):
        install.link_steam(self.game, self.app)
        install.link_steam(self.game, self.app)
        expected = ["argument with spaces", "$(not-a-command)", "single'quote", "-game", "portal"]
        subprocess.run([str(self.launcher), *expected], check=True)
        self.assertEqual(self.args_file.read_text().splitlines(), expected)
        self.assertEqual((self.game / install.BACKUP_NAME).read_bytes(), self.original)
        install.unlink_steam(self.game)
        self.assertEqual(self.launcher.read_bytes(), self.original)
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o751)
        self.assertFalse((self.game / install.BACKUP_NAME).exists())

    def test_backup_collision_does_not_overwrite_anything(self):
        backup = self.game / install.BACKUP_NAME
        backup.write_bytes(b"older backup")
        with self.assertRaisesRegex(RuntimeError, "backup already exists"):
            install.link_steam(self.game, self.app)
        self.assertEqual(backup.read_bytes(), b"older backup")
        self.assertEqual(self.launcher.read_bytes(), self.original)

    def test_restore_refuses_externally_modified_launcher(self):
        install.link_steam(self.game, self.app)
        self.launcher.write_bytes(b"Steam updated this file")
        with self.assertRaisesRegex(RuntimeError, "changed outside"):
            install.unlink_steam(self.game)
        self.assertEqual(self.launcher.read_bytes(), b"Steam updated this file")
        self.assertEqual((self.game / install.BACKUP_NAME).read_bytes(), self.original)

    def test_missing_app_leaves_launcher_untouched(self):
        self.native.unlink()
        with self.assertRaisesRegex(RuntimeError, "Build the native app"):
            install.link_steam(self.game, self.app)
        self.assertEqual(self.launcher.read_bytes(), self.original)
        self.assertFalse((self.game / install.BACKUP_NAME).exists())

    def test_missing_backup_does_not_replace_a_linked_launcher(self):
        install.link_steam(self.game, self.app)
        before = self.launcher.read_bytes()
        (self.game / install.BACKUP_NAME).unlink()
        with self.assertRaisesRegex(RuntimeError, "has no backup"):
            install.link_steam(self.game, self.app)
        self.assertEqual(self.launcher.read_bytes(), before)

    def test_symlink_launcher_does_not_modify_its_target(self):
        other = self.root / "unrelated file"
        other.write_bytes(b"keep me")
        self.launcher.unlink()
        self.launcher.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            install.link_steam(self.game, self.app)
        self.assertEqual(other.read_bytes(), b"keep me")

    @unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64", "requires Apple Silicon Mach-O tools")
    def test_transitive_sdl2_dependency_brings_its_dynamically_loaded_sdl3(self):
        # A tiny real Mach-O application reproduces SDL2-compat's hidden dependency.
        brew = self.root / "fake homebrew"
        lib = brew / "lib"
        lib.mkdir(parents=True)
        sdl3 = brew / "opt/sdl3/lib/libSDL3.dylib"
        sdl3.parent.mkdir(parents=True)
        sdl2 = lib / "libSDL2-2.0.0.dylib"
        res = self.root / "test resources"
        (res / "bin").mkdir(parents=True)
        (res / "portal/bin").mkdir(parents=True)
        source = self.root / "source"
        source.mkdir()
        (source / "sdl3.c").write_text("int ready(void) { return 1; }\n")
        (source / "sdl2.c").write_text('''#include <dlfcn.h>
const char compatibility_marker[] = "SDL2COMPAT";
int test_sdl(void) {
    void *handle = dlopen("@loader_path/libSDL3.dylib", RTLD_NOW);
    if (!handle) return 17;
    dlclose(handle);
    return 0;
}
''')
        (source / "main.c").write_text("extern int test_sdl(void); int main(void) { return test_sdl(); }\n")
        subprocess.run(["clang", "-dynamiclib", str(source / "sdl3.c"), "-o", str(sdl3)], check=True, capture_output=True)
        subprocess.run(["clang", "-dynamiclib", str(source / "sdl2.c"), "-install_name", str(sdl2), "-o", str(sdl2)], check=True, capture_output=True)
        executable = res / "hl2_osx"
        subprocess.run(["clang", str(source / "main.c"), str(sdl2), "-o", str(executable)], check=True, capture_output=True)
        self.assertEqual(subprocess.run([str(executable)]).returncode, 17)
        install.bundle_libraries(res, source, brew)
        shutil.rmtree(brew)
        shutil.rmtree(source)
        self.assertEqual(subprocess.run([str(executable)]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
