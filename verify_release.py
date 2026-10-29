"""Verify checksums, exact archive contents, and the packaged EXE offline."""
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
from package_release import ROOT, VERSION, PORTABLE, SOURCE, sha256, source_files


def verify():
    release = ROOT / "releases"
    manifest = json.loads((release / "checksums.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"CSUFT-Pingan.exe", PORTABLE, SOURCE}, "Unexpected release file set"
    for name, expected in manifest.items():
        path = release / name
        assert path.stat().st_size == expected["bytes"], f"Size mismatch: {name}"
        assert sha256(path) == expected["sha256"], f"Checksum mismatch: {name}"
    sums = "".join(f"{row['sha256']}  {name}\n" for name, row in manifest.items())
    assert (release / "SHA256SUMS.txt").read_text(encoding="ascii") == sums
    forbidden = {"data", "legacy-data", ".venv", "__pycache__", ".idea", ".git", "build", "releases"}
    with ZipFile(release / SOURCE) as archive:
        expected = {f"CSUFT-Pingan-v{VERSION}/{name}" for name in source_files()}
        assert set(archive.namelist()) == expected, "Source archive differs from allowlist"
        for name in source_files():
            assert archive.read(f"CSUFT-Pingan-v{VERSION}/{name}") == (ROOT / name).read_bytes()
    with tempfile.TemporaryDirectory(prefix="pingan-release-离线 ") as folder:
        for name in (SOURCE, PORTABLE):
            with ZipFile(release / name) as archive:
                members = archive.namelist()
                assert len(members) == len(set(members)), "Duplicate archive member"
                for member in members:
                    path = PurePosixPath(member)
                    assert not path.is_absolute() and ".." not in path.parts and "\\" not in member
                    assert not forbidden.intersection(path.parts), f"Private path in archive: {member}"
                    assert not member.endswith((".pyc", ".log", ".bak", ".dpapi", ".pingan-backup"))
                if name == PORTABLE:
                    public_docs = {value for value in source_files() if value.endswith(".md") or
                                   value.startswith("screenshots/") or value == "LICENSE"}
                    core = {value for value in members if not value.startswith("third-party-licenses/")}
                    public_docs = {value for value in public_docs if not value.startswith("third-party-licenses/")}
                    assert core == public_docs | {"CSUFT-Pingan.exe"}, "Unexpected portable files"
                    assert any(value.startswith("third-party-licenses/") for value in members)
                    assert archive.read("CSUFT-Pingan.exe") == (release / "CSUFT-Pingan.exe").read_bytes()
                    archive.extractall(folder)
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PINGAN_API_AUTHORIZATION",
                              "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_SCALE_FACTOR"}}
        windows = Path(os.environ["SystemRoot"])
        env["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
        completed = subprocess.run([str(Path(folder) / "CSUFT-Pingan.exe"), "--demo", "--smoke-test"],
                                   cwd=folder, env=env, timeout=120, capture_output=True)
        assert completed.returncode == 0, "Packaged EXE failed offline startup"
        assert not (Path(folder) / "data").exists(), "Demo created persistent data"
    print(json.dumps({"status": "passed", "checksums": len(manifest),
                      "archives": 2, "packaged_exe_smoke": "passed"}))


if __name__ == "__main__":
    verify()
