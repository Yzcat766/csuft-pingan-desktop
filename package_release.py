"""Package reviewed files only; never recurse through the working directory."""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent
VERSION = "3.4.1"
PORTABLE = f"CSUFT-Pingan-v{VERSION}-windows-x64.zip"
SOURCE = f"CSUFT-Pingan-v{VERSION}-source.zip"
SOURCE_FILES = tuple("""
app.py api.py runner.py storage.py history_storage.py locations.py
position_offset.py overview.py overview_ui.py offline_tests.py
capture_ui.py capture_overview.py capture_forms.py capture_visual.py
form_layout.py ui_components.py tray.py qt_ui.py ui_style.py
ui_resources.py ui_icons.qrc desktop_common.py execution.py
scheduling.py logbook.py log_page.py security.py windows_integration.py
readiness.py notifications.py data_management.py maintenance.py
task_selection.py reliability_ui.py build_exe.py CSUFT-Pingan.spec
package_release.py verify_release.py README.md LICENSE
requirements.txt requirements-build.txt start.cmd install.cmd build.cmd
.gitignore .gitattributes .env.example CONTRIBUTING.md SECURITY.md
CHANGELOG.md THIRD_PARTY_NOTICES.md
assets/chevron-down.svg assets/chevron-up.svg
docs/USAGE.md docs/DEVELOPMENT.md docs/PRIVACY.md
.github/workflows/ci.yml .github/workflows/release.yml
screenshots/overview-dark.png screenshots/overview-light.png
screenshots/accounts.png screenshots/schedule.png
third-party-licenses/Qt-LGPL-3.0-only.txt
third-party-licenses/Qt-GPL-3.0-only.txt
third-party-licenses/Qt-GPL-2.0-only.txt
third-party-licenses/Python-LICENSE.txt
third-party-licenses/SOURCES.md
""".split())


def source_files():
    names = sorted((*SOURCE_FILES, *(p.relative_to(ROOT).as_posix()
                     for p in (ROOT / "tests").glob("test_*.py"))))
    for name in names:
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe source file: {name}")
    return names


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def license_entries():
    """Collect dependency license text; never copy package metadata or scripts."""
    entries = []
    packages = Path(sys.prefix) / "Lib" / "site-packages"
    for folder in sorted(packages.glob("*.dist-info")):
        distribution = folder.name.split("-")[0].lower()
        if distribution in {"pip", "setuptools", "wheel"}:
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and any(word in path.name.upper()
                                       for word in ("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                entries.append((path, "third-party-licenses/" + folder.name + "/" +
                                path.relative_to(folder).as_posix()))
    for path in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"):
        if path.is_file():
            entries.append((path, "third-party-licenses/Python-" + path.name))
    return entries


def package(executable):
    executable = Path(executable).resolve(strict=True)
    if executable.name != "CSUFT-Pingan.exe" or executable.read_bytes()[:2] != b"MZ":
        raise ValueError("Expected a built CSUFT-Pingan.exe Windows executable")
    names = source_files()
    licenses = license_entries()
    if not licenses:
        raise ValueError("Dependency license texts were not found in this Python environment")
    release = ROOT / "releases"
    release.mkdir(exist_ok=True)
    target_exe = release / "CSUFT-Pingan.exe"
    if executable != target_exe:
        shutil.copyfile(executable, target_exe)
    portable_names = [name for name in names if name.endswith(".md") or name.startswith("third-party-licenses/") or
                      name.startswith("screenshots/") or name == "LICENSE"]
    archives = {
        PORTABLE: [(target_exe, "CSUFT-Pingan.exe"),
                   *((ROOT / name, name) for name in portable_names), *licenses],
        SOURCE: [(ROOT / name, f"CSUFT-Pingan-v{VERSION}/{name}") for name in names],
    }
    for name, entries in archives.items():
        temporary = release / (name + ".tmp")
        with ZipFile(temporary, "w", ZIP_DEFLATED, compresslevel=9) as archive:
            for source, member in entries:
                archive.write(source, member)
        temporary.replace(release / name)
    manifest = {path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                for path in [target_exe, *(release / name for name in archives)]}
    (release / "checksums.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (release / "SHA256SUMS.txt").write_text(
        "".join(f"{row['sha256']}  {name}\n" for name, row in manifest.items()), encoding="ascii")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path, help="EXE from a successful build")
    package(parser.parse_args().exe)
