"""Report or enforce non-code gates for a formal Windows release tag."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]

_STATUS_REQUIREMENTS = (
    (REPO_ROOT / "ASSET_LICENSES.md", "Release status: APPROVED"),
    (REPO_ROOT / "THIRD_PARTY_SOURCE.md", "Release status: READY"),
)
_REQUIRED_LICENSE_FILES = (
    "Python-3.12/LICENSE.txt",
    "Qt-PySide6-6.11.1/LGPL-3.0-only.txt",
    "Qt-PySide6-6.11.1/GPL-3.0-only.txt",
    "NumPy-2.4.3/LICENSE.txt",
    "sounddevice-0.5.5/LICENSE.txt",
    "PortAudio/LICENSE.txt",
    "Frida-17.15.3/COPYING.txt",
    "uiautomation-2.0.29/LICENSE.txt",
    "comtypes-1.4.16/LICENSE.txt",
    "pywinrt-3.2.1/LICENSE.txt",
    "cffi-2.1.1/LICENSE.txt",
    "pycparser-3.0/LICENSE.txt",
    "typing_extensions-4.16.0/LICENSE.txt",
    "PyInstaller-6.21.0/COPYING.txt",
    "OpenSSL-3/LICENSE.txt",
)

_PRIVATE_HISTORY_MARKERS = tuple(
    "".join(map(chr, codepoints))
    for codepoints in (
        (0x8A00, 0x7075),
        (0x76, 0x69, 0x62, 0x65, 0x2D, 0x66, 0x6C, 0x6F, 0x77),
        (0x56, 0x69, 0x62, 0x65, 0x20, 0x46, 0x6C, 0x6F, 0x77),
        (0x72, 0x69, 0x63, 0x68, 0x6C, 0x65, 0x61, 0x72, 0x6E, 0x74, 0x6F, 0x64, 0x6F, 0x2D, 0x64, 0x65, 0x62, 0x75, 0x67),
        (0x56, 0x69, 0x62, 0x65, 0x50, 0x61, 0x64),
        (0x4B, 0x65, 0x79, 0x48, 0x6F, 0x70),
        (0x44, 0x3A, 0x5C, 0x57, 0x75, 0x78, 0x69, 0x61, 0x6E, 0x6D, 0x61, 0x69),
        (0x44, 0x3A, 0x5C, 0x43, 0x6C, 0x65, 0x61, 0x72),
    )
)
_PRIVATE_HISTORY_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\"'\s]+", re.IGNORECASE),
)

# PR #14 was already public when merged. The project owner explicitly
# approved retaining this contributor's original email on 2026-10-13.
# Exact immutable commits only; this does not exempt content or subjects.
#
# The 0.1.0-0.1.7 history below was published on this repository before the
# 0.1.7 release. On 2026-10-31 the project owner explicitly approved keeping
# that history as-is instead of rewriting already-published commits, so these
# exact hashes are registered as the same kind of exception. Like above, this
# exempts only the author/committer email of these commits - never their
# subjects or content.
_APPROVED_PUBLIC_CONTRIBUTOR_COMMITS = frozenset({
    "2dc521834df4b30d3492f9ab5355ccf4a125b8c5",
    "c927ac977d6d6d62c1c4976ce2f0af96f7c05c2e",
    "53855b2b6223d7e1f86c396d912a68cfbd86ba8c",
    "7b8fa82303573ffac1f36a67b3389c926654fbfb",
    "9947172eaae77be75fc86827320863ecd61ea7e5",
    "44dd3cbedfe7d7ab74c4842a51e8617d59f5a532",
    "2a1e4f742db5c0e8505475b74029cac82f43ea19",
    "3f3cbcbd0de06da950a6f4a76371a8b295fd330a",
    "e567917d38db00de9652a485fa5c885f2af54c4d",
    "d69896506fa6a7230329a78026e33d24ba7bdca3",
    "256925a9d32f9ae1b7a1f4921e7d39eeab231e5f",
    "9c08ca604594c42a3ff7b183094921b14fbe416a",
    "c47a7792ee8a54ab37489a3cbb3196730b764de9",
    "26a8a4a27a1313b7e44154767baecf0a6f0ad0cc",
    "2e801152c21d7ac81a0b2df24c42b7e13e0868ee",
    "5aa667a9d0e15b01c46930df2a5a930154d45382",
    "8f0c490d8ea616c2857525c1ce549f2be39f3d55",
    "b1eafbf35c5ba141faf9ca17e53489cc054f916f",
    "e26c5d4fcdd3826b2669d1022c3b03e6581d803c",
    "66a29b1e0b26fef1357835c4330ba21cacb78e7b",
    "0ffd7eb100ca2b659a5c6a3c7e8ae1adbc07378b",
    "d77b305f5d42bcdc6a2789ce9da16e99b977687d",
    "69497fbcadf6bf3a01c09b77fd3d778b48707f6f",
    "4d78bdc426201a4eb65f001cebaf04f661c3df00",
    "e1af0f249311c1c7ed4d41d2d0c8c3b194ae5ec5",
    "5eef0a57d246ac587c554cbaf9652ee66f6aade9",
    "83bf21cc06cff9963856a61069379cc4dc4fa32d",
    "bcc5ac9a4401b8ee8f6ec3f618bf9d01852fab94",
    "ef6d91bc203db48f6abb6e95c5a32fe86ad1f4a4",
    "c7930ea400fe5c9a0b995ebd75de75ac937a361b",
    "a9daaba939c23459053e1a66103c1504f29139d6",
})


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _is_public_git_email(value: str) -> bool:
    email = value.strip().casefold()
    return email == "noreply@github.com" or email.endswith("@users.noreply.github.com")


def _git_history_blockers() -> list[str]:
    blockers: list[str] = []
    repository = _run_git("rev-parse", "--show-toplevel")
    if repository.returncode != 0:
        return ["Git history is unavailable for the formal release privacy check"]

    head = _run_git("cat-file", "-e", "HEAD^{commit}")
    if head.returncode != 0:
        return ["Git HEAD is unavailable for the formal release privacy check"]

    revision_range = "HEAD"
    metadata = _run_git(
        "log",
        "--format=%H%x00%ae%x00%ce%x00%s",
        revision_range,
    )
    if metadata.returncode != 0:
        return ["Git history metadata could not be checked"]

    private_email_commits: set[str] = set()
    private_subject_commits: set[str] = set()
    folded_markers = tuple(marker.casefold() for marker in _PRIVATE_HISTORY_MARKERS)
    for line in metadata.stdout.splitlines():
        fields = line.split("\x00", 3)
        if len(fields) != 4:
            blockers.append("Git history metadata contains an unreadable commit record")
            break
        commit, author_email, committer_email, subject = fields
        if commit not in _APPROVED_PUBLIC_CONTRIBUTOR_COMMITS and (
            not _is_public_git_email(author_email)
            or not _is_public_git_email(committer_email)
        ):
            private_email_commits.add(commit)
        folded_subject = subject.casefold()
        if any(marker in folded_subject for marker in folded_markers):
            private_subject_commits.add(commit)

    if private_email_commits:
        blockers.append(
            f"{len(private_email_commits)} commits expose a non-noreply email"
        )
    if private_subject_commits:
        blockers.append(
            f"{len(private_subject_commits)} commit subjects expose internal references"
        )

    private_content_markers = 0
    for marker in _PRIVATE_HISTORY_MARKERS:
        matches = _run_git("log", "--format=%H", "-S", marker, revision_range, "--", ".")
        if matches.returncode != 0:
            blockers.append("Git history content could not be checked")
            break
        if matches.stdout.strip():
            private_content_markers += 1
    if private_content_markers:
        blockers.append(
            f"Git history contains {private_content_markers} internal-reference markers"
        )

    patches = _run_git("log", "--format=", "--patch", revision_range, "--", ".")
    if patches.returncode != 0:
        blockers.append("Git history content could not be checked for private paths")
    elif any(pattern.search(patches.stdout) for pattern in _PRIVATE_HISTORY_PATH_PATTERNS):
        blockers.append("Git history contains a private absolute path")
    return blockers


def release_blockers() -> list[str]:
    blockers: list[str] = []
    for path, required_status in _STATUS_REQUIREMENTS:
        if not path.is_file():
            blockers.append(f"missing release record: {path.name}")
            continue
        text = path.read_text(encoding="utf-8")
        if required_status not in text:
            blockers.append(f"{path.name} has not reached '{required_status}'")

    license_root = REPO_ROOT / "THIRD_PARTY_LICENSES"
    for relative_path in _REQUIRED_LICENSE_FILES:
        if not (license_root / relative_path).is_file():
            blockers.append(f"missing third-party license: {relative_path}")
    blockers.extend(_git_history_blockers())
    return blockers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="return a non-zero exit code when any formal-release blocker remains",
    )
    args = parser.parse_args()

    blockers = release_blockers()
    if blockers:
        print("formal release blockers:")
        for blocker in blockers:
            print(f"- {blocker}")
        return 1 if args.enforce else 0

    print("check-release-readiness: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
