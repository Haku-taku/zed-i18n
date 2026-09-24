"""Stage localized Linux release tarballs into an Arch Linux packaging tree.

The release tarballs are produced by the pinned Linux builder as a portable
``zed.app/`` bundle: binaries carrying an ``$ORIGIN/../lib`` rpath next to a
directory of vendored shared libraries. An Arch package installs into the FHS
layout the official ``zed`` package uses and links against the distribution's
own libraries instead, so the bundle is rewritten rather than copied:

    zed.app/bin/zed              -> usr/bin/zeditor
    zed.app/libexec/zed-editor   -> usr/lib/zed/zed-editor
    zed.app/lib/*.so             -> dropped
    zed.app/licenses.md          -> dropped
    zed.app/share/applications/  -> usr/share/applications/dev.zed.Zed.desktop
    zed.app/share/icons/...      -> usr/share/icons/hicolor/<size>/apps/zed.png

Shell completions and the AppStream metainfo are absent from the bundle and are
generated here, so the output is a complete staging tree. It is consumed by
``packaging/arch/PKGBUILD``, whose ``package()`` only copies ``usr/`` into
``$pkgdir``.
"""

from __future__ import annotations

import gzip
import io
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Iterable

from .deb import (
    DESKTOP_SOURCE_PATTERN,
    ICON_SOURCE_PATTERN,
    GeneratedFile,
    app_root,
    find_linux_tarballs,
    normalized_file_mode,
    read_member,
    rewrite_desktop_entry,
    tar_entry,
)
from .linux_abi import validate_archive_members


# These mirror the official Arch package
# (https://gitlab.archlinux.org/archlinux/packaging/packages/zed). Neither name
# can be changed to match a different $pkgname: crates/cli/src/main.rs resolves
# the editor through a hardcoded "../lib/zed/zed-editor" relative to
# /usr/bin/zeditor, and the comment there records that lib/zed is the Arch
# location as opposed to the libexec/ used elsewhere.
BIN_NAME = "zeditor"
EDITOR_PATH = "usr/lib/zed/zed-editor"
ICON_NAME = "zed"
DESKTOP_FILE_NAME = "dev.zed.Zed.desktop"
METAINFO_FILE_NAME = "dev.zed.Zed.metainfo.xml"

# The official Arch package owns dev.zed.Zed.desktop and this package
# provides/replaces it, so the staged entry claims that ID. This deliberately
# reverses the rule deb.py follows -- a .deb must never install
# dev.zed.Zed.desktop, because it would collide with the official Zed package.
# The Arch package is instead a drop-in replacement for it, so colliding is the
# point. Do not "fix" the two modules into agreement.
METAINFO_SUBSTITUTIONS = {
    "$APP_ID": "dev.zed.Zed",
    "$APP_NAME": "Zed",
    "$BRANDING_LIGHT": "#99c1f1",
    "$BRANDING_DARK": "#1a5fb4",
}

COMPLETION_TARGETS = {
    "bash": f"usr/share/bash-completion/completions/{BIN_NAME}",
    "fish": f"usr/share/fish/vendor_completions.d/{BIN_NAME}.fish",
    "zsh": f"usr/share/zsh/site-functions/_{BIN_NAME}",
}

# Dropped because an Arch package links against the distribution's libraries.
# Discarding lib/ stays safe for the rpath: zed-editor lands in /usr/lib/zed/,
# so its $ORIGIN/../lib resolves to /usr/lib, a standard search path.
DROPPED_BUNDLE_DIRECTORIES = ("lib",)
DROPPED_BUNDLE_FILES = ("licenses.md",)

SUPPORTED_ARCHES = ("x86_64", "aarch64")

METAINFO_TEMPLATE = Path(__file__).with_name("arch_overlay") / "zed.metainfo.xml.in"

_HOST_ARCHES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


def arch_asset_name(language: str | None, arch: str) -> str:
    # Universal assets (language=None) drop the locale segment entirely.
    locale_segment = f"-{language}" if language else ""
    return f"zed-i18n{locale_segment}-linux-{arch}-pkgdir.tar.gz"


def host_arch() -> str | None:
    """Return this machine's Zed architecture name, or None if unrecognized."""
    return _HOST_ARCHES.get(platform.machine().lower())


def render_metainfo(template_path: Path = METAINFO_TEMPLATE) -> str:
    """Substitute the packaging identity into the AppStream template.

    Mirrors the official PKGBUILD's prepare(): envsubst the identity variables,
    then drop the ``@release_info@`` placeholder while keeping the dummy release
    entry that ships in the Arch package.
    """
    text = template_path.read_text(encoding="utf-8")
    for variable, value in METAINFO_SUBSTITUTIONS.items():
        text = text.replace(variable, value)
    lines = [line for line in text.splitlines() if "@release_info@" not in line]
    return "\n".join(lines) + "\n"


def extract_bundle(tarball_path: Path, destination: Path) -> str:
    """Extract a release tarball and return the name of its top-level directory."""
    with tarfile.open(tarball_path, "r:gz") as archive:
        members = archive.getmembers()
        validate_archive_members(members)
        root = app_root(members)
        archive.extractall(destination, members=members, filter="fully_trusted")
    return root


def generate_completions(
    tarball_path: Path,
    shells: Iterable[str] = tuple(COMPLETION_TARGETS),
) -> dict[str, str]:
    """Run the bundled CLI to capture its shell completions.

    Completion scripts describe the CLI's argument parser, which does not vary
    by architecture, so one run covers every Linux build in a release.

    The CLI has to be invoked under the name the package installs it as. clap
    derives the generated function name, the zsh ``#compdef`` line and the fish
    ``complete -c`` target from argv[0], so running the bundle's ``bin/zed``
    yields completions for a command called ``zed`` -- which would never fire
    for the installed ``zeditor``. The copy stays beside the original so the
    CLI's own ``../libexec/zed-editor`` lookup still resolves.
    """
    with tempfile.TemporaryDirectory(prefix="zed-i18n-completions-") as temp_dir:
        workdir = Path(temp_dir)
        root = extract_bundle(tarball_path, workdir)
        launcher = workdir / root / "bin" / "zed"
        if not launcher.is_file():
            raise ValueError(f"release archive has no bin/zed launcher: {tarball_path.name}")
        launcher.chmod(0o755)

        cli = launcher.with_name(BIN_NAME)
        shutil.copy2(launcher, cli)

        # A throwaway HOME keeps the CLI from reading or writing a real profile.
        home = workdir / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home)}

        completions = {}
        for shell in shells:
            result = subprocess.run(
                [str(cli), "--completions", shell],
                check=True,
                capture_output=True,
                text=True,
                env=env,
                cwd=workdir,
            )
            if not result.stdout.strip():
                raise ValueError(
                    f"the bundled CLI produced no {shell} completions for {tarball_path.name}"
                )
            completions[shell] = result.stdout
        return completions


def build_arch_from_tarball(
    tarball_path: Path,
    output_path: Path,
    *,
    arch: str,
    completions: dict[str, str],
    metainfo: str,
) -> Path:
    if arch not in SUPPORTED_ARCHES:
        raise ValueError(f"unsupported Linux architecture: {arch}")

    entries: list[tuple[str, tarfile.TarInfo | None, GeneratedFile | None]] = []
    generated: list[GeneratedFile] = []
    dropped_libraries: list[str] = []

    # The input archive stays open for the whole build: the staged tree is
    # written with a streaming tar writer, which needs each member's payload
    # available as it is added.
    with tarfile.open(tarball_path, "r:gz") as archive:
        members = archive.getmembers()
        validate_archive_members(members)
        bundle_root = app_root(members)
        mtime = max((member.mtime for member in members), default=0)

        desktop_sources: list[tarfile.TarInfo] = []
        icon_sources: list[tuple[str, tarfile.TarInfo]] = []

        for member in members:
            if member.isdir():
                continue
            relative = str(PurePosixPath(member.name).relative_to(bundle_root))
            if PurePosixPath(relative).parts[0] in DROPPED_BUNDLE_DIRECTORIES:
                if member.isfile():
                    dropped_libraries.append(relative)
                continue
            if relative in DROPPED_BUNDLE_FILES:
                continue
            if not member.isfile():
                raise ValueError(f"unexpected non-file member in bundle: {member.name}")

            if relative == "bin/zed":
                entries.append((f"usr/bin/{BIN_NAME}", member, None))
                continue
            if relative == "libexec/zed-editor":
                entries.append((EDITOR_PATH, member, None))
                continue
            if DESKTOP_SOURCE_PATTERN.fullmatch(relative):
                desktop_sources.append(member)
                continue
            icon_match = ICON_SOURCE_PATTERN.fullmatch(relative)
            if icon_match:
                icon_sources.append((icon_match.group("size"), member))
                continue
            # Fail loudly rather than silently dropping something upstream added.
            raise ValueError(f"unrecognized member in Linux bundle: {member.name}")

        if len(desktop_sources) != 1:
            names = [member.name for member in desktop_sources]
            raise ValueError(f"expected exactly one desktop entry in bundle, found: {names}")
        if not icon_sources:
            raise ValueError("Linux bundle contains no application icons")

        generated.append(
            GeneratedFile(
                f"usr/share/applications/{DESKTOP_FILE_NAME}",
                rewrite_desktop_entry(
                    read_member(archive, desktop_sources[0]).decode("utf-8"),
                    BIN_NAME,
                    ICON_NAME,
                ).encode("utf-8"),
                0o644,
            )
        )
        for size, member in icon_sources:
            generated.append(
                GeneratedFile(
                    f"usr/share/icons/hicolor/{size}/apps/{ICON_NAME}.png",
                    read_member(archive, member),
                    0o644,
                )
            )

        for shell, target in COMPLETION_TARGETS.items():
            text = completions.get(shell)
            if text is None:
                raise ValueError(f"missing generated {shell} completions")
            generated.append(GeneratedFile(target, text.encode("utf-8"), 0o644))

        generated.append(
            GeneratedFile(
                f"usr/share/metainfo/{METAINFO_FILE_NAME}",
                metainfo.encode("utf-8"),
                0o644,
            )
        )

        entries.extend((entry.name, None, entry) for entry in generated)

        file_names = [name for name, _, _ in entries]
        duplicates = sorted({name for name in file_names if file_names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate package paths: {duplicates}")

        directories: set[str] = {"usr/"}
        for name in file_names:
            parent = PurePosixPath(name).parent
            while str(parent) != ".":
                directories.add(f"{parent}/")
                parent = parent.parent

        if dropped_libraries:
            print(
                f"Dropped {len(dropped_libraries)} bundled libraries from {tarball_path.name} "
                "(the Arch package uses system libraries): " + ", ".join(dropped_libraries)
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="zed-i18n-arch-", dir=output_path.parent
        ) as temp_dir:
            partial_path = Path(temp_dir) / output_path.name
            # Streamed rather than buffered: the payload is a ~130 MB binary
            # bundle. An explicit mtime and an empty gzip filename keep the
            # output byte-identical for a given input.
            with partial_path.open("wb") as raw:
                with gzip.GzipFile(
                    filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed, mode="w|", format=tarfile.GNU_FORMAT
                    ) as tar:
                        for name in sorted(directories):
                            tar.addfile(
                                tar_entry(name, mode=0o755, mtime=mtime, typ=tarfile.DIRTYPE)
                            )
                        for name, member, generated_file in sorted(
                            entries, key=lambda item: item[0]
                        ):
                            if member is not None:
                                info = tar_entry(
                                    name,
                                    mode=normalized_file_mode(member.mode),
                                    mtime=member.mtime,
                                )
                                info.size = member.size
                                source = archive.extractfile(member)
                                if source is None:
                                    raise ValueError(
                                        f"cannot read archive member: {member.name}"
                                    )
                                tar.addfile(info, source)
                            else:
                                assert generated_file is not None
                                info = tar_entry(name, mode=generated_file.mode, mtime=mtime)
                                info.size = len(generated_file.data)
                                tar.addfile(info, io.BytesIO(generated_file.data))
            partial_path.replace(output_path)

    return output_path


def _completion_source(tarballs: list[tuple[Path, str | None, str]], locale: str | None) -> Path:
    candidates = [(path, arch) for path, tar_locale, arch in tarballs if tar_locale == locale]
    native = host_arch()
    for path, arch in candidates:
        if arch == native:
            return path
    arches = sorted({arch for _, arch in candidates})
    raise ValueError(
        f"cannot generate shell completions for {locale or 'the universal build'}: "
        f"no Linux archive for this runner's architecture ({native}), found {arches}. "
        "Include the matching Linux shard, e.g. --platforms linux-x86_64."
    )


def _build_one(job: tuple[str, str, str, dict[str, str], str]) -> str:
    tarball, output, arch, completions, metainfo = job
    build_arch_from_tarball(
        Path(tarball),
        Path(output),
        arch=arch,
        completions=completions,
        metainfo=metainfo,
    )
    return output


def build_release_arch(dist_dir: Path, *, max_workers: int | None = None) -> list[Path]:
    """Turn every localized Linux release tarball into an Arch staging tarball."""
    tarballs = find_linux_tarballs(dist_dir)
    if not tarballs:
        return []

    metainfo = render_metainfo()
    completions: dict[str | None, dict[str, str]] = {}
    for _path, locale, _arch in tarballs:
        if locale not in completions:
            completions[locale] = generate_completions(_completion_source(tarballs, locale))

    jobs = [
        (
            str(path),
            str(dist_dir / arch_asset_name(locale, arch)),
            arch,
            completions[locale],
            metainfo,
        )
        for path, locale, arch in tarballs
    ]

    if len(jobs) == 1 or (max_workers is not None and max_workers <= 1):
        return [Path(_build_one(job)) for job in jobs]

    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        return [Path(output) for output in executor.map(_build_one, jobs)]
