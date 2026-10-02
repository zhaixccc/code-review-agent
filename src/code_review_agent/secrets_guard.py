"""Keep credentials away from the model: sensitive-file detection, secret scanning and redaction.

Nothing here ever returns or logs the secret value itself; only its kind and line number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Finding

_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tpl")
_SENSITIVE_NAMES = {
    ".npmrc", ".pypirc", ".netrc", ".htpasswd", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json", "secrets.yml", "secrets.yaml", "service-account.json",
    "terraform.tfstate", "kubeconfig",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx", ".ppk", ".tfstate")


def is_sensitive_path(filename: str) -> bool:
    """Files that normally hold secrets must never be sent to a third-party model."""
    name = PurePosixPath(filename.replace("\\", "/")).name.lower()
    if name.endswith(_SAFE_SUFFIXES):
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    return name in _SENSITIVE_NAMES or name.endswith(_SENSITIVE_SUFFIXES)


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]


_RULES = (
    _Rule("私钥", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY(?: BLOCK)?-----")),
    _Rule("AWS Access Key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _Rule("GitHub Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    _Rule("Slack Token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    _Rule("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    _Rule("API Key（sk- 前缀）", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    _Rule("Bearer Token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{24,}")),
)
_ASSIGNMENT = re.compile(
    r"""(?ix)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b\s*[:=]\s*["']([^"'\s]{12,})["']"""
)
_PLACEHOLDER = re.compile(r"(?i)change.?me|example|placeholder|your[_-]|dummy|sample|xxxx|\*{4,}|<.+>|\$\{|\{\{|process\.env|os\.environ|getenv")


def _looks_like_secret(value: str) -> bool:
    if _PLACEHOLDER.search(value):
        return False
    classes = sum(bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3


def _match_kind(line: str) -> str | None:
    for rule in _RULES:
        if rule.pattern.search(line):
            return rule.kind
    assignment = _ASSIGNMENT.search(line)
    if assignment and _looks_like_secret(assignment.group(1)):
        return "硬编码凭据"
    return None


def redact(text: str) -> tuple[str, int]:
    """Replace secrets in text with a marker; returns (text, number_of_redactions)."""
    count = 0
    lines = []
    for line in text.splitlines():
        kind = _match_kind(line)
        if kind is None:
            lines.append(line)
            continue
        count += 1
        redacted = line
        for rule in _RULES:
            redacted = rule.pattern.sub(f"[已脱敏:{rule.kind}]", redacted)
        redacted = _ASSIGNMENT.sub(lambda m: m.group(0).replace(m.group(1), "[已脱敏:硬编码凭据]"), redacted)
        lines.append(redacted)
    return "\n".join(lines), count


def scan_added_lines(annotated_patch: str) -> list[tuple[int, str]]:
    """Return (new_file_line, kind) for each added line in an annotated patch that looks like a secret."""
    hits: list[tuple[int, str]] = []
    for raw in annotated_patch.splitlines():
        match = re.match(r"^\s*(\d+) \+ (.*)$", raw)
        if not match:
            continue
        kind = _match_kind(match.group(2))
        if kind:
            hits.append((int(match.group(1)), kind))
    return hits


def secret_finding(kind: str, line: int | None) -> Finding:
    return Finding(
        origin="rule",
        severity="critical",
        category="security",
        line=line,
        title=f"疑似提交了{kind}",
        detail="该改动包含疑似凭据，内容已在发送给模型前脱敏；一旦推送到远端，应视为已泄露。",
        suggestion="立即轮换该凭据，从代码和 Git 历史中移除，改用环境变量或密钥管理服务。",
    )


def sensitive_file_finding(filename: str) -> Finding:
    return Finding(
        origin="rule",
        severity="critical",
        category="security",
        line=None,
        title=f"敏感文件被提交：{PurePosixPath(filename).name}",
        detail="此类文件通常包含密钥或证书；其内容未发送给模型，但文件已进入仓库历史。",
        suggestion="确认其中是否有真实凭据，若有请立即轮换，并把该文件加入 .gitignore、从历史中清除。",
    )
