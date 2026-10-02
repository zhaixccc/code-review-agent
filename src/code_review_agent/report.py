"""Render the review as GitHub-flavoured Markdown (with output sanitising)."""

from __future__ import annotations

import re
from typing import Any

from .models import SEVERITY_ORDER, FileReview, Finding

MARKER = "<!-- code-review-agent -->"

_MENTION_RE = re.compile(r"@(?=[A-Za-z0-9_-])")
_RISKY_TAG_RE = re.compile(r"<(?=/?(?:img|a|iframe|script|style|picture|source|video|audio|object|embed|link|meta|svg)\b|!--)", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[")

VERDICT_TEXT = {
    "request_changes": "建议修改后再合并（存在严重或较大风险）",
    "comment": "有若干建议，请作者评估",
    "approve": "未发现明显问题",
}


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
    head = f"**[{finding.severity}/{finding.category}]** {sanitize(finding.title)}"
    lines = [f"- {head}" + (f" — {location}" if with_location else "")]
    lines.append(f"  {sanitize(finding.detail)}")
    if finding.suggestion:
        lines.append(f"  建议：{sanitize(finding.suggestion)}")
    return "\n".join(lines)


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
) -> str:
    items = collect(file_reviews)
    counts = {severity: sum(1 for _, finding in items if finding.severity == severity) for severity in SEVERITY_ORDER}
    parts = [MARKER, f"## 自动代码审查（`{sha[:7]}`）", f"**结论：** {VERDICT_TEXT.get(verdict, verdict)}"]
    if summary:
        parts.append(sanitize(summary))
    if items:
        parts.append("**问题统计：** " + " · ".join(f"{name} {count}" for name, count in counts.items() if count))
        parts.append("### 发现的问题")
        for path, finding in items:
            anchored_inline = inline_paths is not None and finding.line is not None and (path, finding.line) in inline_paths
            if anchored_inline:
                continue
            parts.append(render_finding(path, finding))
        if inline_paths and any(finding.line and (path, finding.line) in inline_paths for path, finding in items):
            parts.append("_部分问题已作为行内评论发布。_")
    else:
        parts.append("本次提交的可审查改动中没有发现需要报告的问题。")
    reviewed = len(file_reviews)
    notes = [f"已审查 {reviewed} 个文件"]
    if skipped:
        notes.append(f"跳过 {len(skipped)} 个文件（{'; '.join(skipped[:5])}{' …' if len(skipped) > 5 else ''}）")
    if errors:
        notes.append(f"{len(errors)} 个审查步骤失败，结果可能不完整")
    parts.append("---")
    parts.append("<sub>" + "；".join(sanitize(note) for note in notes) + f"。由 LangGraph + DeepSeek（{model}）自动生成，仅供参考，请人工复核。</sub>")
    return "\n\n".join(parts)


def build_inline_comments(file_reviews: list[FileReview]) -> list[dict[str, Any]]:
    """Inline review comments only for findings anchored on added lines that exist in the diff."""
    comments: list[dict[str, Any]] = []
    for review in file_reviews:
        valid = set(review.valid_lines)
        for finding in review.findings:
            if finding.line and finding.line in valid:
                body = f"**[{finding.severity}/{finding.category}]** {sanitize(finding.title)}\n\n{sanitize(finding.detail)}"
                if finding.suggestion:
                    body += f"\n\n建议：{sanitize(finding.suggestion)}"
                comments.append({"path": review.filename, "line": finding.line, "side": "RIGHT", "body": body})
    return comments
