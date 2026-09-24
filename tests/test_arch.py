import io
import shutil
import tarfile
import unittest
from pathlib import Path

from tools.zed_i18n.arch import (
    DESKTOP_FILE_NAME,
    METAINFO_FILE_NAME,
    arch_asset_name,
    build_arch_from_tarball,
    build_release_arch,
    render_metainfo,
)


FIXTURE_MTIME = 1_700_000_000

FIXTURE_DESKTOP = (
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=Zed i18n\n"
    "GenericName=Text Editor\n"
    "TryExec=zed\n"
    "Exec=zed %U\n"
    "Icon=zed\n"
    "Actions=NewWorkspace;\n"
    "\n"
    "[Desktop Action NewWorkspace]\n"
    "Exec=zed --new %U\n"
    "Name=Open a new workspace\n"
)

# Stands in for the real CLI: the staging step runs `<name> --completions
# <shell>` and captures stdout, so a canned emitter exercises the same path
# without needing a Zed build. It reports the name it was invoked as, because
# the real CLI names its completions after argv[0] -- running it as `zed`
# produces completions for a command called `zed`, which would never fire for
# the installed `zeditor`.
FIXTURE_CLI = (
    "#!/bin/sh\n"
    'if [ "$1" != "--completions" ]; then\n'
    '  echo "unexpected arguments: $*" >&2\n'
    "  exit 1\n"
    "fi\n"
    'echo "# canned $2 completion for $(basename "$0")"\n'
)

COMPLETIONS = {
    "bash": "# canned bash completion for zeditor\n",
    "fish": "# canned fish completion for zeditor\n",
    "zsh": "# canned zsh completion for zeditor\n",
}

EXPECTED_FILES = {
    "usr/bin/zeditor",
    "usr/lib/zed/zed-editor",
    f"usr/share/applications/{DESKTOP_FILE_NAME}",
    f"usr/share/metainfo/{METAINFO_FILE_NAME}",
    "usr/share/icons/hicolor/512x512/apps/zed.png",
    "usr/share/icons/hicolor/1024x1024/apps/zed.png",
    "usr/share/bash-completion/completions/zeditor",
    "usr/share/fish/vendor_completions.d/zeditor.fish",
    "usr/share/zsh/site-functions/_zeditor",
}


class ArchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path.cwd() / "tests" / ".tmp" / self._testMethodName
        shutil.rmtree(self.temp_root, ignore_errors=True)
        self.temp_root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def add_member(
        self,
        tar: tarfile.TarFile,
        name: str,
        data: bytes = b"",
        mode: int = 0o644,
        typ: bytes = tarfile.REGTYPE,
    ) -> None:
        info = tarfile.TarInfo(name)
        info.mtime = FIXTURE_MTIME
        info.mode = mode
        info.type = typ
        if typ == tarfile.REGTYPE:
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        else:
            tar.addfile(info)

    def make_tarball(
        self,
        path: Path,
        desktop_name: str = "dev.zed-i18n.Zed.desktop",
        cli: bytes = FIXTURE_CLI.encode("utf-8"),
        include_icons: bool = True,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as tar:
            self.add_member(tar, "zed.app", mode=0o755, typ=tarfile.DIRTYPE)
            self.add_member(tar, "zed.app/bin", mode=0o755, typ=tarfile.DIRTYPE)
            self.add_member(tar, "zed.app/bin/zed", cli, mode=0o755)
            self.add_member(tar, "zed.app/libexec/zed-editor", b"editor-binary", mode=0o755)
            self.add_member(tar, "zed.app/lib/libstdc++.so.6", b"bundled-lib")
            self.add_member(tar, "zed.app/lib/libssl.so.3", b"bundled-lib")
            self.add_member(
                tar,
                f"zed.app/share/applications/{desktop_name}",
                FIXTURE_DESKTOP.encode("utf-8"),
                mode=0o755,
            )
            if include_icons:
                self.add_member(
                    tar, "zed.app/share/icons/hicolor/512x512/apps/zed.png", b"png512"
                )
                self.add_member(
                    tar, "zed.app/share/icons/hicolor/1024x1024/apps/zed.png", b"png1024"
                )
            self.add_member(tar, "zed.app/licenses.md", b"bundled licenses")
        return path

    def build(self, tarball: Path | None = None, arch: str = "x86_64") -> Path:
        tarball = tarball or self.make_tarball(self.temp_root / "in" / "zed-linux.tar.gz")
        return build_arch_from_tarball(
            tarball,
            self.temp_root / "out" / arch_asset_name(None, arch),
            arch=arch,
            completions=COMPLETIONS,
            metainfo=render_metainfo(),
        )

    def read_members(self, path: Path) -> dict[str, bytes]:
        with tarfile.open(path, "r:gz") as tar:
            return {
                member.name: tar.extractfile(member).read()
                for member in tar.getmembers()
                if member.isfile()
            }

    def test_arch_asset_name_keeps_release_asset_convention(self) -> None:
        self.assertEqual(
            arch_asset_name("ko-KR", "x86_64"),
            "zed-i18n-ko-KR-linux-x86_64-pkgdir.tar.gz",
        )
        self.assertEqual(
            arch_asset_name(None, "aarch64"),
            "zed-i18n-linux-aarch64-pkgdir.tar.gz",
        )

    def test_stages_the_official_arch_layout(self) -> None:
        members = self.read_members(self.build())
        self.assertEqual(set(members), EXPECTED_FILES)
        # The bundle's own copies must not survive under their original names.
        self.assertNotIn("usr/bin/zed", members)
        self.assertNotIn("usr/lib/zed/libexec/zed-editor", members)

    def test_places_binaries_on_the_paths_the_cli_looks_up(self) -> None:
        members = self.read_members(self.build())
        self.assertEqual(members["usr/bin/zeditor"], FIXTURE_CLI.encode("utf-8"))
        self.assertEqual(members["usr/lib/zed/zed-editor"], b"editor-binary")

    def test_normalizes_file_modes(self) -> None:
        with tarfile.open(self.build(), "r:gz") as tar:
            modes = {
                member.name: member.mode
                for member in tar.getmembers()
                if member.isfile()
            }
        self.assertEqual(modes["usr/bin/zeditor"], 0o755)
        self.assertEqual(modes["usr/lib/zed/zed-editor"], 0o755)
        self.assertEqual(modes[f"usr/share/applications/{DESKTOP_FILE_NAME}"], 0o644)
        self.assertEqual(modes["usr/share/icons/hicolor/512x512/apps/zed.png"], 0o644)

    def test_claims_the_official_desktop_identity(self) -> None:
        members = self.read_members(self.build())
        desktop = members[f"usr/share/applications/{DESKTOP_FILE_NAME}"].decode("utf-8")
        self.assertIn("Exec=zeditor %U\n", desktop)
        self.assertIn("Exec=zeditor --new %U\n", desktop)
        self.assertIn("TryExec=zeditor\n", desktop)
        self.assertIn("Icon=zed\n", desktop)
        # deb.py forbids this ID outright; the Arch package claims it instead.
        self.assertNotIn(
            "usr/share/applications/dev.zed-i18n.Zed.desktop",
            members,
        )

    def test_renames_icons_to_the_official_name(self) -> None:
        members = self.read_members(self.build())
        self.assertEqual(members["usr/share/icons/hicolor/512x512/apps/zed.png"], b"png512")
        self.assertEqual(
            members["usr/share/icons/hicolor/1024x1024/apps/zed.png"], b"png1024"
        )

    def test_drops_bundled_libraries_and_licenses(self) -> None:
        members = self.read_members(self.build())
        self.assertFalse([name for name in members if name.startswith("usr/lib/zed/lib")])
        self.assertNotIn("usr/lib/zed/licenses.md", members)
        self.assertNotIn("usr/share/licenses/licenses.md", members)

    def test_generates_completions_and_metainfo(self) -> None:
        members = self.read_members(self.build())
        self.assertEqual(
            members["usr/share/bash-completion/completions/zeditor"].decode("utf-8"),
            COMPLETIONS["bash"],
        )
        self.assertEqual(
            members["usr/share/fish/vendor_completions.d/zeditor.fish"].decode("utf-8"),
            COMPLETIONS["fish"],
        )
        self.assertEqual(
            members["usr/share/zsh/site-functions/_zeditor"].decode("utf-8"),
            COMPLETIONS["zsh"],
        )
        metainfo = members[f"usr/share/metainfo/{METAINFO_FILE_NAME}"].decode("utf-8")
        self.assertIn("<id>dev.zed.Zed</id>", metainfo)
        self.assertIn("<name>Zed</name>", metainfo)
        self.assertIn(
            '<launchable type="desktop-id">dev.zed.Zed.desktop</launchable>', metainfo
        )
        self.assertNotIn("@release_info@", metainfo)
        self.assertNotIn("$APP_ID", metainfo)

    def test_output_is_deterministic(self) -> None:
        tarball = self.make_tarball(self.temp_root / "in" / "zed-linux.tar.gz")
        first = self.build(tarball).read_bytes()
        second = self.build(tarball).read_bytes()
        self.assertEqual(first, second)

    def test_rejects_a_bundle_without_icons(self) -> None:
        tarball = self.make_tarball(
            self.temp_root / "in" / "zed-linux.tar.gz", include_icons=False
        )
        with self.assertRaises(ValueError) as caught:
            self.build(tarball)
        self.assertIn("no application icons", str(caught.exception))

    def test_rejects_an_unrecognized_bundle_member(self) -> None:
        tarball = self.temp_root / "in" / "zed-linux.tar.gz"
        tarball.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball, "w:gz") as tar:
            self.add_member(tar, "zed.app", mode=0o755, typ=tarfile.DIRTYPE)
            self.add_member(tar, "zed.app/bin/zed", FIXTURE_CLI.encode("utf-8"), mode=0o755)
            self.add_member(tar, "zed.app/bin/zed-remote-server", b"server", mode=0o755)
        with self.assertRaises(ValueError) as caught:
            self.build(tarball)
        self.assertIn("unrecognized member", str(caught.exception))

    def test_rejects_an_unsupported_architecture(self) -> None:
        tarball = self.make_tarball(self.temp_root / "in" / "zed-linux.tar.gz")
        with self.assertRaises(ValueError) as caught:
            build_arch_from_tarball(
                tarball,
                self.temp_root / "out" / "x.tar.gz",
                arch="riscv64",
                completions=COMPLETIONS,
                metainfo=render_metainfo(),
            )
        self.assertIn("unsupported Linux architecture", str(caught.exception))

    def test_render_metainfo_matches_the_official_prepare_step(self) -> None:
        metainfo = render_metainfo()
        self.assertIn("<id>dev.zed.Zed</id>", metainfo)
        self.assertIn('<color type="primary" scheme_preference="light">#99c1f1</color>', metainfo)
        self.assertIn('<color type="primary" scheme_preference="dark">#1a5fb4</color>', metainfo)
        # The official PKGBUILD deletes the placeholder but keeps the dummy
        # release entry that precedes it.
        self.assertNotIn("@release_info@", metainfo)
        self.assertIn('<release version="0.0.0" date="1970-01-01">', metainfo)

    def test_build_release_arch_returns_nothing_without_linux_archives(self) -> None:
        dist_dir = self.temp_root / "dist"
        dist_dir.mkdir()
        self.assertEqual(build_release_arch(dist_dir), [])

    def test_build_release_arch_stages_every_linux_archive(self) -> None:
        dist_dir = self.temp_root / "dist"
        for arch in ("x86_64", "aarch64"):
            self.make_tarball(dist_dir / f"zed-i18n-linux-{arch}.tar.gz")

        built = build_release_arch(dist_dir)

        self.assertEqual(
            sorted(path.name for path in built),
            ["zed-i18n-linux-aarch64-pkgdir.tar.gz", "zed-i18n-linux-x86_64-pkgdir.tar.gz"],
        )
        for path in built:
            self.assertEqual(set(self.read_members(path)), EXPECTED_FILES)

    def test_build_release_arch_ignores_its_own_output(self) -> None:
        # find_linux_tarballs anchors on .tar.gz before the -pkgdir suffix, so a
        # dist dir that already holds staged trees -- a re-run, or a workflow
        # that builds debs and Arch trees in the same directory -- still finds
        # exactly the two release archives and nothing else.
        dist_dir = self.temp_root / "dist"
        for arch in ("x86_64", "aarch64"):
            self.make_tarball(dist_dir / f"zed-i18n-linux-{arch}.tar.gz")
        self.make_tarball(dist_dir / "zed-i18n-linux-x86_64-pkgdir.tar.gz")
        (dist_dir / "zed-i18n-linux-x86_64.deb").write_bytes(b"deb")

        built = build_release_arch(dist_dir)

        self.assertEqual(
            sorted(path.name for path in built),
            ["zed-i18n-linux-aarch64-pkgdir.tar.gz", "zed-i18n-linux-x86_64-pkgdir.tar.gz"],
        )


if __name__ == "__main__":
    unittest.main()
