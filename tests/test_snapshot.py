"""Safe archive extraction and the snapshot cache (no network)."""

import io
import tarfile
from pathlib import Path

import pytest

from code_review_agent.github_client import GitHubError
from code_review_agent.snapshot import GitHubSnapshotProvider, extract_source_archive

SHA = "a" * 40


def make_archive(path: Path, members: list[tuple[str, bytes | None, str]]) -> None:
    """members: (name, data, kind) where kind is file | symlink | dir."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(data or b"")
                tar.addfile(info, io.BytesIO(data or b""))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside.py"
                tar.addfile(info)
            else:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)


def test_only_safe_source_files_are_extracted(tmp_path):
    archive = tmp_path / "a.tar.gz"
    make_archive(
        archive,
        [
            ("o-r-abc/", None, "dir"),
            ("o-r-abc/src/app.py", b"def f():\n    pass\n", "file"),
            ("o-r-abc/web/app.ts", b"export const x = 1;\n", "file"),
            ("o-r-abc/node_modules/lib/index.js", b"x", "file"),  # vendored directory
            ("o-r-abc/logo.png", b"png", "file"),  # not a source file
            ("o-r-abc/link.py", None, "symlink"),  # links are never followed
            ("o-r-abc/../escape.py", b"evil", "file"),  # path traversal
            ("o-r-abc/big.py", b"x" * 5000, "file"),  # over the per-file limit below
        ],
    )
    out = tmp_path / "out"
    count = extract_source_archive(archive, out, max_file_bytes=1000)
    assert count == 2
    assert (out / "src" / "app.py").read_text() == "def f():\n    pass\n"
    assert (out / "web" / "app.ts").exists()
    assert not (tmp_path / "escape.py").exists() and not (out / "escape.py").exists()
    assert not (out / "node_modules").exists() and not (out / "link.py").exists() and not (out / "big.py").exists()


def test_file_count_limit_stops_extraction(tmp_path):
    archive = tmp_path / "a.tar.gz"
    make_archive(archive, [(f"o-r-abc/m{i}.py", b"x = 1\n", "file") for i in range(10)])
    assert extract_source_archive(archive, tmp_path / "out", max_files=3) == 3


class FakeGitHub:
    def __init__(self, source: Path | None = None, fail: bool = False):
        self.source, self.fail, self.downloads = source, fail, 0

    def download_archive(self, repo, sha, destination, max_bytes):
        self.downloads += 1
        if self.fail:
            raise GitHubError("HTTP 404", 404)
        destination.write_bytes(self.source.read_bytes())
        return destination.stat().st_size


def test_provider_downloads_once_and_reuses_the_snapshot(tmp_path):
    archive = tmp_path / "src.tar.gz"
    make_archive(archive, [("o-r-abc/pkg/mod.py", b"def f():\n    pass\n", "file")])
    github = FakeGitHub(archive)
    provider = GitHubSnapshotProvider(github, tmp_path / "cache")
    first = provider.get("o/r", SHA)
    second = provider.get("o/r", SHA)
    assert first == second and (first / "pkg" / "mod.py").exists()
    assert github.downloads == 1
    assert not any(p.name.startswith(".tmp-") for p in (tmp_path / "cache").iterdir())  # scratch space is cleaned up


def test_provider_returns_none_when_the_download_fails(tmp_path):
    provider = GitHubSnapshotProvider(FakeGitHub(fail=True), tmp_path / "cache")
    assert provider.get("o/r", SHA) is None


def test_provider_rejects_invalid_identifiers(tmp_path):
    provider = GitHubSnapshotProvider(FakeGitHub(), tmp_path / "cache")
    with pytest.raises(ValueError):
        provider.get("../etc", SHA)
    with pytest.raises(ValueError):
        provider.get("o/r", "not-a-sha")


def test_only_the_newest_snapshots_are_kept(tmp_path):
    archive = tmp_path / "src.tar.gz"
    make_archive(archive, [("o-r-abc/a.py", b"x = 1\n", "file")])
    provider = GitHubSnapshotProvider(FakeGitHub(archive), tmp_path / "cache", keep=2)
    import os
    import time

    for index, char in enumerate("123"):
        path = provider.get("o/r", char * 40)
        stamp = time.time() + index * 10
        os.utime(path / ".snapshot-complete", (stamp, stamp))
    provider._evict()
    remaining = sorted(p.name for p in (tmp_path / "cache").iterdir())
    assert len(remaining) == 2 and not any("1" * 40 in name for name in remaining)
