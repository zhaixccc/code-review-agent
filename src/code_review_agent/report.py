"""Render the review as GitHub-flavoured Markdown (with output sanitising)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .models import SEVERITY_ORDER, FileReview, Finding

MARKER = "<!-- code-review-agent -->"
INLINE_MARKER = "<!-- cra-inline"
_FP_RE = re.compile(r"<!-- cra-inline fp=([0-9a-f]{12}) -->")

_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9_-])")
_RISKY_TAG_RE = re.compile(r"<(?=/?(?:img|a|iframe|script|style|picture|source|video|audio|object|embed|link|meta|svg)\b|!--)", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[")

VERDICT_TEXT = {
    "request_changes": "建议修改后再合并（存在严重或较大风险）",
    "comment": "有若干建议，请作者评估",
    "approve": "未发现明显问题",
}
PRIORITY_BY_SEVERITY = {"critical": "P0", "major": "P1", "minor": "P2", "nit": "P3"}
PRIORITY_ORDER = ("P0", "P1", "P2", "P3")
SEVERITY_LABEL = {"critical": "紧急", "major": "高", "minor": "中", "nit": "提示"}
CATEGORY_LABEL = {
    "bug": "缺陷",
    "security": "安全",
    "performance": "性能",
    "maintainability": "可维护性",
    "testing": "测试",
    "style": "规范",
}


def priority_for(severity: str) -> str:
    """Map review severity to an explicit triage priority for humans."""
    return PRIORITY_BY_SEVERITY.get(severity, "P3")


def sanitize(text: str) -> str:
    """Model output is influenced by untrusted code: neutralise @mentions, remote images and risky HTML."""
    text = _MENTION_RE.sub("@\u200b", text)
    text = _IMAGE_RE.sub("!\\[", text)
    return _RISKY_TAG_RE.sub("&lt;", text)


def verdict_for(findings: list[Finding]) -> str:
    severities = {finding.severity for finding in findings}
    if severities & {"critical", "major"}:
        return "request_changes"
    return "comment" if findings else "approve"


def collect(file_reviews: list[FileReview]) -> list[tuple[str, Finding]]:
    items = [(review.filename, finding) for review in file_reviews for finding in review.findings]
    return sorted(items, key=lambda item: (SEVERITY_ORDER[item[1].severity], item[0], item[1].line or 0))


def render_finding(path: str, finding: Finding, with_location: bool = True) -> str:
    location = f"`{path}:{finding.line}`" if finding.line else f"`{path}`"
    severity = SEVERITY_LABEL.get(finding.severity, finding.severity)
    category = CATEGORY_LABEL.get(finding.category, finding.category)
    head = f"**[{priority_for(finding.severity)} · {severity}/{category}]** {sanitize(finding.title)}"
    lines = [f"- {head}" + (f" — {location}" if with_location else "")]
    lines.append(f"  {sanitize(finding.detail)}")
    if finding.suggestion:
        lines.append(f"  建议：{sanitize(finding.suggestion)}")
    return "\n".join(lines)


def fingerprint(path: str, finding: Finding) -> str:
    """Stable across line shifts and re-runs: the same problem in the same file gets the same id."""
    normalized = re.sub(r"[\W_]+", "", finding.title.lower())
    return hashlib.sha1(f"{path}|{finding.category}|{normalized}".encode("utf-8")).hexdigest()[:12]


def fingerprint_from_body(body: str) -> str | None:
    match = _FP_RE.search(body)
    return match.group(1) if match else None


def existing_fingerprints(comments: list[dict[str, Any]], me: str) -> tuple[set[str], set[tuple[str, int]]]:
    """Fingerprints and (path, line) anchors of this agent's earlier inline comments on a pull request."""
    fingerprints: set[str] = set()
    anchors: set[tuple[str, int]] = set()
    for comment in comments:
        body = str(comment.get("body", ""))
        if INLINE_MARKER not in body or str((comment.get("user") or {}).get("login", "")).lower() != me.lower():
            continue
        match = _FP_RE.search(body)
        if match:
            fingerprints.add(match.group(1))
        line = comment.get("line") or comment.get("original_line")
        if comment.get("path") and line:
            anchors.add((str(comment["path"]), int(line)))
    return fingerprints, anchors


def render_report(
    *,
    sha: str,
    verdict: str,
    summary: str,
    file_reviews: list[FileReview],
    skipped: list[str],
    errors: list[str],
    model: str,
    inline_paths: set[tuple[str, int]] | None = None,
    memory_used: int = 0,
    impact_summary: list[str] | None = None,
    verify_dropped: int = 0,
    verify_downgraded: int = 0,
    inline_omitted_count: int = 0,
) -> str:
    items = collect(file_reviews)
    counts = {severity: sum(1 for _, finding in items if finding.severity == severity) for severity in SEVERITY_ORDER}
    parts = [MARKER, f"## 自动代码审查（`{sha[:7]}`）", f"**结论：** {VERDICT_TEXT.get(verdict, verdict)}"]
    if summary:
        parts.append(sanitize(summary))
    if impact_summary:
        parts.append("### 影响面\n" + "\n".join(f"- {sanitize(line)}" for line in impact_summary))
    if items:
        parts.append("**问题统计：** " + " · ".join(
            f"{SEVERITY_LABEL.get(name, name)} {count}" for name, count in counts.items() if count
        ))
        priority_counts = {
            priority: sum(1 for _, finding in items if priority_for(finding.severity) == priority)
            for priority in PRIORITY_ORDER
        }
        parts.append("**处置优先级：** " + " · ".join(f"{priority} {count}" for priority, count in priority_counts.items() if count))
        parts.append("### 发现的问题")
        for priority in PRIORITY_ORDER:
            group = [(path, finding) for path, finding in items if priority_for(finding.severity) == priority]
            visible = [
                (path, finding)
                for path, finding in group
                if inline_paths is None or finding.line is None or (path, finding.line) not in inline_paths
            ]
            if not visible:
                continue
            parts.append(f"#### {priority}")
            parts.extend(render_finding(path, finding) for path, finding in visible)
        inline_published_count = sum(
            1 for path, finding in items
            if inline_paths is not None and finding.line is not None and (path, finding.line) in inline_paths
        )
        if inline_published_count:
            parts.append(f"_已发布 {inline_published_count} 条行内评论；P3 nit 仅列于汇总，避免行内评论噪声。_")
        elif any(finding.severity == "nit" for _, finding in items):
            parts.append("_P3 nit 仅列于汇总，避免行内评论噪声。_")
    else:
        parts.append("本次提交的可审查改动中没有发现需要报告的问题。")
    reviewed = len(file_reviews)
    notes = [f"已审查 {reviewed} 个文件"]
    if skipped:
        notes.append(f"跳过 {len(skipped)} 个文件（{'; '.join(skipped[:5])}{' …' if len(skipped) > 5 else ''}）")
    if errors:
        notes.append(f"{len(errors)} 个审查步骤失败，结果可能不完整")
    if memory_used:
        notes.append(f"参考了 {memory_used} 条项目记忆（Hindsight）")
    if verify_dropped or verify_downgraded:
        notes.append(f"二次验证：剔除 {verify_dropped} 条、降级 {verify_downgraded} 条未能证实的问题")
    if inline_omitted_count:
        notes.append(f"另有 {inline_omitted_count} 条问题未能作为行内评论发布，已保留在本汇总中")
    parts.append("---")
    parts.append("<sub>" + "；".join(sanitize(note) for note in notes) + f"。由 LangGraph + DeepSeek（{model}）自动生成，仅供参考，请人工复核。</sub>")
    return "\n\n".join(parts)


def build_inline_comments(file_reviews: list[FileReview]) -> list[dict[str, Any]]:
    """Inline review comments only for findings anchored on added lines that exist in the diff."""
    comments: list[dict[str, Any]] = []
    for review in file_reviews:
        valid = set(review.valid_lines)
        for finding in review.findings:
            # Nits remain visible in the priority-grouped summary, but do not create noisy inline threads.
            if finding.severity == "nit":
                continue
            if finding.line and finding.line in valid:
                severity = SEVERITY_LABEL.get(finding.severity, finding.severity)
                category = CATEGORY_LABEL.get(finding.category, finding.category)
                body = f"{INLINE_MARKER} fp={fingerprint(review.filename, finding)} -->\n**[{priority_for(finding.severity)} · {severity}/{category}]** {sanitize(finding.title)}\n\n{sanitize(finding.detail)}"
                if finding.suggestion:
                    body += f"\n\n建议：{sanitize(finding.suggestion)}"
                comments.append({"path": review.filename, "line": finding.line, "side": "RIGHT", "body": body})
    return comments
