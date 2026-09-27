"""Rules for deciding which files are safe to publish from this repository."""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


_MODEL_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".gguf",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
}
_AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_CACHE_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
    "node_modules",
    "venv",
}
_MAX_PUBLICATION_FILE_BYTES = 2 * 1024 * 1024
_PERSONAL_HOME = re.compile(r"/home/(?!<user>(?:/|$)|\$\{?[A-Z_]+\}?/)[A-Za-z0-9._-]+(?:/|$)")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*[A-Z0-9_.-]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)"
    r"[A-Z0-9_.-]*[ \t]*[:=][ \t]*['\"]?([A-Za-z0-9+/_.=-]{20,})"
)
_SECRET_PATTERNS = (
    (re.compile(r"cfast_[A-Za-z0-9]{48}"), "credential-like Cloudflare service secret"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "credential-like API key"),
    (re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"), "private key material"),
)


@dataclass(frozen=True)
class Violation:
    path: Path
    reason: str


def path_violations(path: Path) -> list[str]:
    """Return publication problems encoded by a repository-relative path."""

    value = path.as_posix()
    normalized = PurePosixPath(value[2:] if value.startswith("./") else value)
    lowered = normalized.as_posix().lower()
    parts = {part.lower() for part in normalized.parts}
    suffix = normalized.suffix.lower()
    reasons: list[str] = []

    if normalized.name == ".env" or (
        normalized.name.endswith(".env") and normalized.name != ".env.example"
    ):
        reasons.append("active environment file")
    if normalized.name.startswith(".env.") and normalized.name != ".env.example":
        reasons.append("active environment file")
    if "runtime" in parts:
        reasons.append("runtime data")
    if suffix in _DATABASE_SUFFIXES:
        reasons.append("database")
    if suffix in _MODEL_SUFFIXES:
        reasons.append("model artifact")
    if suffix in _AUDIO_SUFFIXES:
        reasons.append("generated audio")
    if parts & _CACHE_PARTS or suffix in {".pyc", ".pyo"}:
        reasons.append("cache")
    if suffix == ".pdf" and "competition handbook" in parts:
        reasons.append("competition PDF")
    if "cloudflare" in parts and normalized.name != "access-policy.example.json":
        if suffix in {".pem", ".crt", ".key"} or (
            suffix == ".json" and not normalized.name.endswith(".example.json")
        ):
            reasons.append("Cloudflare credential")
    if lowered in {
        "config/models.yaml",
        "deploy/cloudflare/current-state.json",
        "deploy/cloudflare/tunnel-config.yml",
    }:
        reasons.append("host-specific configuration")

    return list(dict.fromkeys(reasons))


def content_violations(content: str) -> list[str]:
    """Return publication problems without ever returning the matched value."""

    reasons: list[str] = []
    if _PERSONAL_HOME.search(content):
        reasons.append("personal absolute path")
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(content):
            reasons.append(reason)
    if _CREDENTIAL_ASSIGNMENT.search(content):
        reasons.append("credential-like assigned value")
    return reasons


def _git_candidates(root: Path) -> list[Path] | None:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return sorted(
        Path(item.decode("utf-8", errors="surrogateescape"))
        for item in completed.stdout.split(b"\0")
        if item
    )


def committed_candidates(root: Path, treeish: str = "HEAD") -> list[Path]:
    """List file paths stored in a committed Git tree."""

    completed = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", treeish],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to inspect the committed Git tree")
    return sorted(
        Path(item.decode("utf-8", errors="surrogateescape"))
        for item in completed.stdout.split(b"\0")
        if item
    )


def _ignore_patterns(root: Path) -> list[str]:
    ignore_file = root / ".gitignore"
    if not ignore_file.is_file():
        return []
    return [
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _matches_ignore(path: Path, patterns: Sequence[str]) -> bool:
    value = path.as_posix()
    ignored = False
    for raw_pattern in patterns:
        negated = raw_pattern.startswith("!")
        pattern = raw_pattern[1:] if negated else raw_pattern
        pattern = pattern.lstrip("/")
        directory_pattern = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        matches = fnmatch.fnmatch(value, pattern) or fnmatch.fnmatch(path.name, pattern)
        if directory_pattern:
            matches = matches or value == pattern or value.startswith(pattern + "/")
        if matches:
            ignored = not negated
    return ignored


def publication_candidates(root: Path, *, prefer_git: bool = True) -> list[Path]:
    """List tracked and unignored files that would form the publication tree."""

    root = root.resolve()
    if prefer_git:
        candidates = _git_candidates(root)
        if candidates is not None:
            return candidates

    patterns = _ignore_patterns(root)
    candidates: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if ".git" in relative.parts or _matches_ignore(relative, patterns):
            continue
        candidates.append(relative)
    return sorted(candidates)


def inspect_candidates(root: Path, candidates: Iterable[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for relative in candidates:
        path_reasons = path_violations(relative)
        for reason in path_reasons:
            violations.append(Violation(relative, reason))
        if path_reasons:
            continue

        candidate = root / relative
        try:
            if candidate.stat().st_size > _MAX_PUBLICATION_FILE_BYTES:
                violations.append(Violation(relative, "file exceeds publication size limit"))
                continue
            content = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for reason in content_violations(content):
            violations.append(Violation(relative, reason))
    return violations


def inspect_committed_tree(
    root: Path, candidates: Iterable[Path], treeish: str = "HEAD"
) -> list[Violation]:
    """Inspect path names and text blobs exactly as stored in a Git tree."""

    violations: list[Violation] = []
    for relative in candidates:
        path_reasons = path_violations(relative)
        for reason in path_reasons:
            violations.append(Violation(relative, reason))
        if path_reasons:
            continue

        object_name = f"{treeish}:{relative.as_posix()}"
        size_result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-s", object_name],
            check=False,
            capture_output=True,
            text=True,
        )
        if size_result.returncode != 0:
            violations.append(Violation(relative, "unreadable committed blob"))
            continue
        if int(size_result.stdout.strip()) > _MAX_PUBLICATION_FILE_BYTES:
            violations.append(Violation(relative, "file exceeds publication size limit"))
            continue

        completed = subprocess.run(
            ["git", "-C", str(root), "show", object_name],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            violations.append(Violation(relative, "unreadable committed blob"))
            continue
        try:
            content = completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for reason in content_violations(content):
            violations.append(Violation(relative, reason))
    return violations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--filesystem",
        action="store_true",
        help="inspect the filesystem instead of Git's tracked/unignored candidates",
    )
    modes.add_argument(
        "--committed",
        action="store_true",
        help="inspect paths and blobs exactly as committed at HEAD",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    if args.committed:
        candidates = committed_candidates(root)
        violations = inspect_committed_tree(root, candidates)
        subject = "committed file(s)"
    else:
        candidates = publication_candidates(root, prefer_git=not args.filesystem)
        violations = inspect_candidates(root, candidates)
        subject = "candidate file(s)"
    if violations:
        print(f"repository boundary failed: {len(violations)} violation(s)")
        for violation in violations:
            print(f"- {violation.path.as_posix()}: {violation.reason}")
        return 1
    print(f"repository boundary passed: {len(candidates)} {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
