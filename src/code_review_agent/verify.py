"""Second-pass verification: a skeptical model call tries to refute critical/major/minor model findings.

Why: one model pass produces plausible-sounding false positives, and a verdict of "request changes" built on them
makes people ignore the bot. Verification only ever *removes or lowers* findings; it never adds any, and it
fails open (a failed call keeps the finding unchanged). Every decision is returned in a log for auditability.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .config import Settings
from .diff_utils import annotate_patch, trim_patch
from .llm import extract_json
from .models import SEVERITY_ORDER, ChangedFile, FileReview, Finding
from .prompts import IMPACT_BLOCK, VERIFY_SYSTEM, VERIFY_USER
from .secrets_guard import redact

logger = logging.getLogger(__name__)

_DOWNGRADE = {"critical": "major", "major": "minor", "minor": "nit"}
_ROW_NUMBER = re.compile(r"^\s*(\d+)[ +]")
_CONTEXT_LINES = 15
_MAX_EXCERPT_CHARS = 4000
_MAX_IMPACT_CHARS = 3000


@dataclass
class VerifyResult:
    reviews: list[FileReview]
    log: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    downgraded: int = 0
    failed: int = 0


def _excerpt(file: ChangedFile | None, line: int | None, max_patch_chars: int) -> str:
    if file is None or not file.patch:
        return ""
    trimmed, _ = trim_patch(file.patch, max_patch_chars)
    rows = annotate_patch(trimmed)[0].splitlines()
    chosen = rows[:60]
    if line is not None:
        near = [
            index
            for index, row in enumerate(rows)
            if (match := _ROW_NUMBER.match(row)) and abs(int(match.group(1)) - line) <= _CONTEXT_LINES
        ]
        if near:
            chosen = rows[near[0] : near[-1] + 1]
    text, _ = redact("\n".join(chosen))
    return text[:_MAX_EXCERPT_CHARS]


def _decide(llm: BaseChatModel, settings: Settings, filename: str, finding: Finding, code: str, impact: str) -> tuple[str, str] | None:
    impact_block = IMPACT_BLOCK.format(impact=impact[:_MAX_IMPACT_CHARS]) if impact else ""
    user = VERIFY_USER.format(
        filename=filename,
        severity=finding.severity,
        category=finding.category,
        line=finding.line if finding.line is not None else "unknown",
        title=finding.title,
        detail=finding.detail,
        impact_block=impact_block,
        code=code or "(no code excerpt available)",
    )
    try:
        response = llm.invoke(
            [SystemMessage(content=VERIFY_SYSTEM.format(language=settings.review_language)), HumanMessage(content=user)]
        )
    except Exception as error:
        logger.warning("Verification request failed for %s: %s", filename, type(error).__name__)
        return None
    data = extract_json(response.content) or {}
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in {"confirmed", "refuted", "uncertain"}:
        return None
    return verdict, str(data.get("reason", ""))[:300]


def verify_reviews(
    llm: BaseChatModel, settings: Settings, reviews: list[FileReview], files: dict[str, ChangedFile], impact: dict[str, str]
) -> VerifyResult:
    candidates: list[tuple[int, int, Finding]] = [
        (r, f, finding)
        for r, review in enumerate(reviews)
        for f, finding in enumerate(review.findings)
        if finding.origin == "model" and finding.severity in _DOWNGRADE
    ]
    candidates.sort(key=lambda item: (SEVERITY_ORDER[item[2].severity], reviews[item[0]].filename, item[2].line or 0))
    candidates = candidates[: settings.verify_max_findings]
    result = VerifyResult(reviews=[review.model_copy(deep=True) for review in reviews])
    if not candidates:
        return result

    def work(item: tuple[int, int, Finding]) -> tuple[tuple[int, int, Finding], tuple[str, str] | None]:
        r, _, finding = item
        name = reviews[r].filename
        code = _excerpt(files.get(name), finding.line, settings.max_patch_chars_per_file)
        return item, _decide(llm, settings, name, finding, code, impact.get(name, ""))

    with ThreadPoolExecutor(max_workers=max(1, settings.llm_concurrency)) as pool:
        decisions = list(pool.map(work, candidates))

    remove: set[tuple[int, int]] = set()
    for (r, f, finding), decision in decisions:
        entry: dict[str, Any] = {"file": reviews[r].filename, "title": finding.title, "severity": finding.severity}
        if decision is None:
            result.failed += 1
            entry.update(verdict="unverified", reason="verification call failed or returned no verdict")
        else:
            verdict, reason = decision
            entry.update(verdict=verdict, reason=reason)
            if verdict == "refuted":
                remove.add((r, f))
                result.dropped += 1
            elif verdict == "uncertain":
                result.reviews[r].findings[f] = finding.model_copy(update={"severity": _DOWNGRADE[finding.severity]})
                result.downgraded += 1
        result.log.append(entry)
    for r, review in enumerate(result.reviews):
        review.findings = [finding for f, finding in enumerate(review.findings) if (r, f) not in remove]
    return result
