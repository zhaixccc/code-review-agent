"""Risk-based file selection under a size budget (large changes are not reviewed front-to-back)."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .models import ChangedFile

_HIGH_RISK_TOKENS = (
    "auth", "login", "logout", "password", "passwd", "token", "secret", "crypto", "cipher", "encrypt", "hash", "sign",
    "permission", "acl", "rbac", "admin", "payment", "billing", "checkout", "sql", "query", "database", "db",
    "migration", "exec", "shell", "command", "deserial", "upload", "download", "session", "cookie", "oauth", "jwt",
    "webhook", "middleware", "security", "sanitize", "validate", "parser", "router", "handler", "controller", "lock",
    "thread", "mutex", "concurrent", "async",
)
_CONFIG_NAMES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile", "nginx.conf", "pom.xml", "build.gradle"}
_CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb",
    ".php", ".swift", ".scala", ".sh", ".ps1", ".sql", ".vue", ".lua", ".dart",
}
_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec|e2e)(/|$)|(_test|\.test|\.spec)\.[a-z0-9]+$|(^|/)test_[^/]+$", re.IGNORECASE)


def risk_score(file: ChangedFile) -> float:
    """Heuristic, deterministic review priority: higher means review it first."""
    path = file.filename.replace("\\", "/")
    pure = PurePosixPath(path.lower())
    score = 0.0
    if pure.suffix in _CODE_SUFFIXES:
        score += 3.0
    if pure.name in _CONFIG_NAMES or path.lower().startswith(".github/workflows/"):
        score += 3.5
    if pure.suffix in _DOC_SUFFIXES:
        score -= 2.5
    if _TEST_RE.search(path):
        score -= 1.5
    haystack = "/".join(pure.parts)
    score += min(4.0, 1.0 * sum(token in haystack for token in _HIGH_RISK_TOKENS))
    # More changed lines mean more room for mistakes, with diminishing returns.
    score += min(3.0, (file.additions + 0.5 * file.deletions) / 60)
    return score


def select_files(
    files: list[ChangedFile], *, max_files: int, max_total_chars: int, max_file_chars: int
) -> tuple[list[ChangedFile], list[str]]:
    """Pick the highest-risk files that fit the budget; return (selected, skip reasons)."""
    ranked = sorted(files, key=lambda f: (-risk_score(f), f.filename))
    selected: list[ChangedFile] = []
    skipped: list[str] = []
    used = 0
    for file in ranked:
        cost = min(len(file.patch or ""), max_file_chars)
        if len(selected) >= max_files:
            skipped.append(f"{file.filename}: 超过单次审查文件上限")
        elif used + cost > max_total_chars and selected:
            skipped.append(f"{file.filename}: 超过本次审查的 diff 总量预算")
        else:
            selected.append(file)
            used += cost
    return selected, skipped
