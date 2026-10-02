"""The LangGraph review workflow.

fetch_changes -> triage -> impact_analysis -> recall_memory -> (fan out: one review_file per file, in parallel)
              -> verify -> synthesize -> publish -> learn

* fetch_changes   : read the commit / pull request files from GitHub (paginated) and redact secrets in the message
* triage          : sensitive files become deterministic findings (never sent to the model); the rest are ranked by
                    risk and fitted into a size budget instead of being taken front-to-back
* impact_analysis : download the repository at the reviewed commit, parse it with tree-sitter and find the callers and
                    tests of the symbols the diff touches (optional; evidence for the reviewer, not a verdict)
* recall_memory   : ask Hindsight for conventions and maintainer feedback relevant to the change (optional)
* review_file     : DeepSeek reviews one file in chunks with the impact evidence; secrets are redacted first and found
                    secrets become deterministic findings; every finding is validated against the real diff
* verify          : a skeptical second pass tries to refute each major/critical finding; refuted ones are dropped
* synthesize      : merge/sort findings, derive the verdict from severities, ask DeepSeek for a short summary
* publish         : update-or-create the commit comment (push) or PR summary comment plus new inline comments (PR)
* learn           : feed trusted conventions and maintainer ratings back into Hindsight (skipped on dry runs)
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .cache import ReviewCache
from .config import Settings
from .diff_utils import annotate_patch, chunk_text, should_review, trim_patch
from .github_client import GitHubClient, GitHubError
from .impact import analyze_impact
from .isolation import run_isolated
from .llm import extract_json
from .memory import ProjectMemory
from .models import ChangedFile, FileReview, Finding, ReviewTarget
from .prioritize import select_files
from .prompts import (
    FILE_REVIEW_SYSTEM,
    FILE_REVIEW_USER,
    IMPACT_BLOCK,
    MEMORY_BLOCK,
    PROMPT_VERSION,
    SUMMARY_SYSTEM,
    SUMMARY_USER,
)
from .report import (
    MARKER,
    build_inline_comments,
    collect,
    existing_fingerprints,
    fingerprint_from_body,
    render_report,
    verdict_for,
)
from .secrets_guard import is_sensitive_path, redact, scan_added_lines, secret_finding, sensitive_file_finding
from .snapshot import SnapshotProvider
from .verify import verify_reviews

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_CHUNK = 8
MAX_FINDINGS_PER_FILE = 12
MAX_SECRET_FINDINGS_PER_FILE = 5
MAX_MESSAGE_CHARS = 2000


class ReviewState(TypedDict, total=False):
    target: dict[str, Any]
    commit_message: str
    changed_paths: list[str]
    files: list[dict[str, Any]]
    skipped: list[str]
    impact: dict[str, str]
    impact_summary: list[str]
    memory: str
    memory_count: int
    # Parallel branches append to these lists; the reducer merges them.
    file_reviews: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    # verify replaces (rather than appends to) the findings, so it writes its own key.
    verified_reviews: list[dict[str, Any]]
    verify_log: list[dict[str, Any]]
    verify_dropped: int
    verify_downgraded: int
    verdict: str
    summary: str
    report: str
    posted: bool


class FileTask(TypedDict):
    file: dict[str, Any]
    commit_message: str
    memory: str
    impact: str


def _parse_findings(data: dict[str, Any] | None) -> list[Finding]:
    findings: list[Finding] = []
    raw_items = data.get("findings") if data else None
    if not isinstance(raw_items, list):
        return findings
    for item in raw_items[:MAX_FINDINGS_PER_CHUNK]:
        try:
            # origin is decided by code, never by the model (a model must not be able to mark itself as a rule).
            findings.append(Finding.model_validate(item).model_copy(update={"origin": "model"}))
        except Exception:  # one malformed finding must not discard the rest
            continue
    return findings


def _final_reviews(state: ReviewState) -> list[FileReview]:
    source = state.get("verified_reviews")
    if source is None:
        source = state.get("file_reviews", [])
    return [FileReview.model_validate(item) for item in source]


def build_graph(
    settings: Settings,
    github: GitHubClient,
    llm: BaseChatModel,
    *,
    dry_run: bool = False,
    memory: ProjectMemory | None = None,
    snapshots: SnapshotProvider | None = None,
    cache: ReviewCache | None = None,
):
    """Compile the review graph. Dependencies are injected so tests can use fakes."""

    def fetch_changes(state: ReviewState) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        if target.pr_number is not None:
            files = github.get_pull_request_files(target.repo, target.pr_number, settings.max_commit_pages)
            message = github.get_pull_request_title(target.repo, target.pr_number)
        else:
            message, files = github.get_commit(target.repo, target.sha, settings.max_commit_pages)
        safe_message, _ = redact(message)
        return {
            "commit_message": safe_message[:MAX_MESSAGE_CHARS],
            "files": [file.model_dump() for file in files],
            "changed_paths": [file.filename for file in files],
        }

    def triage(state: ReviewState) -> dict[str, Any]:
        candidates: list[ChangedFile] = []
        skipped: list[str] = []
        sensitive: list[dict[str, Any]] = []
        for raw in state.get("files", []):
            file = ChangedFile.model_validate(raw)
            if file.status == "removed":
                skipped.append(f"{file.filename}: 已删除")
            elif is_sensitive_path(file.filename):
                # Never send these to a third-party model; the commit itself is the finding.
                sensitive.append(FileReview(filename=file.filename, findings=[sensitive_file_finding(file.filename)]).model_dump())
                skipped.append(f"{file.filename}: 敏感文件，内容未发送给模型")
            elif not should_review(file.filename):
                skipped.append(f"{file.filename}: 生成/二进制/依赖文件")
            elif not file.patch:
                skipped.append(f"{file.filename}: 无 diff（二进制或过大）")
            else:
                candidates.append(file)
        selected, over_budget = select_files(
            candidates,
            max_files=settings.max_files_per_review,
            max_total_chars=settings.max_total_patch_chars,
            max_file_chars=settings.max_patch_chars_per_file,
        )
        update: dict[str, Any] = {"files": [file.model_dump() for file in selected], "skipped": skipped + over_budget}
        if sensitive:
            update["file_reviews"] = sensitive
        return update

    def impact_analysis(state: ReviewState) -> dict[str, Any]:
        """Evidence about who depends on the touched symbols. Best effort: any failure means "no evidence"."""
        files = state.get("files", [])
        if snapshots is None or not settings.impact_enabled or not files:
            return {}
        target = ReviewTarget.model_validate(state["target"])
        try:
            root = snapshots.get(target.repo, target.sha)
            if root is None:
                return {}
            arguments = (root, [ChangedFile.model_validate(f) for f in files], set(state.get("changed_paths", [])), settings)
            if settings.impact_isolated:
                # Parsing untrusted files with native code: a crash or hang only costs this review's evidence.
                report = run_isolated(analyze_impact, arguments, settings.impact_time_budget_seconds + 20)
            else:
                report = analyze_impact(*arguments)
        except Exception as error:  # never let static analysis fail a review
            logger.warning("Impact analysis failed (%s); reviewing without it.", type(error).__name__)
            return {}
        logger.info("Impact analysis for %s@%s: %s", target.repo, target.sha[:7], report.stats)
        return {"impact": report.by_file, "impact_summary": report.summary}

    def recall_memory(state: ReviewState) -> dict[str, Any]:
        files = state.get("files", [])
        if memory is None or not files:
            return {"memory": "", "memory_count": 0}
        target = ReviewTarget.model_validate(state["target"])
        context = memory.recall_for_review(target.repo, [f["filename"] for f in files], state.get("commit_message", ""))
        return {"memory": context.text, "memory_count": context.count}

    def dispatch(state: ReviewState) -> list[Send] | str:
        files = state.get("files", [])
        if not files:
            return "verify"
        message, recalled, impact = state.get("commit_message", ""), state.get("memory", ""), state.get("impact", {})
        return [
            Send("review_file", {"file": file, "commit_message": message, "memory": recalled, "impact": impact.get(file["filename"], "")})
            for file in files
        ]

    def review_file(task: FileTask) -> dict[str, Any]:
        file = ChangedFile.model_validate(task["file"])
        trimmed, omitted_hunks = trim_patch(file.patch or "", settings.max_patch_chars_per_file)
        truncated = omitted_hunks > 0 or len(trimmed) < len(file.patch or "")
        annotated, valid_lines = annotate_patch(trimmed)

        # Deterministic secret findings come from the raw text; the model only ever sees the redacted text.
        deterministic: list[Finding] = []
        seen_secrets: set[tuple[int, str]] = set()
        for line, kind in scan_added_lines(annotated):
            if (line, kind) not in seen_secrets and len(deterministic) < MAX_SECRET_FINDINGS_PER_FILE:
                seen_secrets.add((line, kind))
                deterministic.append(secret_finding(kind, line))
        safe_text, _ = redact(annotated)

        chunks = chunk_text(safe_text, settings.max_chunk_chars)
        memory_text, impact_text = task.get("memory", ""), task.get("impact", "")
        memory_block = MEMORY_BLOCK.format(memory=memory_text) if memory_text else ""
        impact_block = IMPACT_BLOCK.format(impact=impact_text) if impact_text else ""
        truncated_note = ""
        if truncated:
            truncated_note = f" (diff truncated: {omitted_hunks} hunk(s) in the middle are not shown)" if omitted_hunks else " (diff truncated)"
        model_findings: list[Finding] = []
        errors: list[str] = []
        cache_key = None
        if cache is not None:
            cache_key = cache.key(
                settings.deepseek_model, PROMPT_VERSION, settings.review_language, file.filename, safe_text,
                task.get("commit_message", ""), memory_text, impact_text,
            )
            cached = cache.get(cache_key)
            if cached is not None:
                try:
                    model_findings = [Finding.model_validate(item).model_copy(update={"origin": "model"}) for item in cached]
                    chunks = []  # nothing to ask the model
                except Exception:
                    model_findings = []
        system = FILE_REVIEW_SYSTEM.format(language=settings.review_language)
        for index, chunk in enumerate(chunks, start=1):
            user = FILE_REVIEW_USER.format(
                filename=file.filename,
                index=index,
                total=len(chunks),
                truncated=truncated_note,
                message=task.get("commit_message", ""),
                memory_block=memory_block,
                impact_block=impact_block,
                diff=chunk,
            )
            try:
                response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
            except Exception as error:  # network/API failure: record type only, never the payload
                logger.warning("LLM request failed for %s: %s", file.filename, type(error).__name__)
                errors.append(f"{file.filename}: 模型请求失败（{type(error).__name__}）")
                continue
            parsed = extract_json(response.content)
            if parsed is None:
                errors.append(f"{file.filename}: 模型未返回有效 JSON")
                continue
            model_findings.extend(_parse_findings(parsed))
        if cache is not None and cache_key is not None and chunks and not errors:
            cache.put(cache_key, [finding.model_dump() for finding in model_findings])
        findings: list[Finding] = [*deterministic, *model_findings]

        # Only keep line anchors that really exist on added lines; otherwise report without a line.
        seen: set[tuple[int | None, str]] = set()
        cleaned: list[Finding] = []
        for finding in findings:
            if finding.line is not None and finding.line not in valid_lines:
                finding = finding.model_copy(update={"line": None})
            key = (finding.line, finding.title.strip().lower())
            if key not in seen:
                seen.add(key)
                cleaned.append(finding)
        review = FileReview(
            filename=file.filename,
            findings=cleaned[:MAX_FINDINGS_PER_FILE],
            valid_lines=sorted(valid_lines),
            truncated=truncated,
        )
        update: dict[str, Any] = {"errors": errors}
        # A file whose every chunk failed is not "reviewed" unless deterministic findings exist for it.
        if not errors or len(errors) < len(chunks) or deterministic:
            update["file_reviews"] = [review.model_dump()]
        return update

    def verify(state: ReviewState) -> dict[str, Any]:
        """Second pass: drop findings a skeptical reviewer can refute, lower the ones it cannot confirm."""
        reviews = [FileReview.model_validate(item) for item in state.get("file_reviews", [])]
        if not settings.verify_findings or not reviews:
            return {}
        files = {item["filename"]: ChangedFile.model_validate(item) for item in state.get("files", [])}
        try:
            result = verify_reviews(llm, settings, reviews, files, state.get("impact", {}))
        except Exception as error:  # verification only ever removes noise; its failure must not fail the review
            logger.warning("Verification step failed (%s); publishing unverified findings.", type(error).__name__)
            return {}
        if result.log:
            logger.info(
                "Verification: %d checked, %d dropped, %d downgraded, %d unverified",
                len(result.log), result.dropped, result.downgraded, result.failed,
            )
        return {
            "verified_reviews": [review.model_dump() for review in result.reviews],
            "verify_log": result.log,
            "verify_dropped": result.dropped,
            "verify_downgraded": result.downgraded,
        }

    def synthesize(state: ReviewState) -> dict[str, Any]:
        reviews = _final_reviews(state)
        items = collect(reviews)
        findings = [finding for _, finding in items]
        verdict = verdict_for(findings)
        summary = ""
        if findings:
            payload = "\n".join(f"- [{f.severity}/{f.category}] {path}:{f.line or '-'} {f.title}" for path, f in items[:40])
            try:
                response = llm.invoke(
                    [
                        SystemMessage(content=SUMMARY_SYSTEM.format(language=settings.review_language)),
                        HumanMessage(content=SUMMARY_USER.format(message=state.get("commit_message", ""), findings=payload)),
                    ]
                )
                data = extract_json(response.content) or {}
                summary = str(data.get("summary", ""))[:1500]
            except Exception as error:
                logger.warning("Summary request failed: %s", type(error).__name__)
        return {"verdict": verdict, "summary": summary}

    def publish(state: ReviewState) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        reviews = _final_reviews(state)

        def render(inline_keys: set[tuple[str, int]] | None = None) -> str:
            return render_report(
                sha=target.sha,
                verdict=state.get("verdict", "comment"),
                summary=state.get("summary", ""),
                file_reviews=reviews,
                skipped=state.get("skipped", []),
                errors=state.get("errors", []),
                model=settings.deepseek_model,
                inline_paths=inline_keys,
                memory_used=int(state.get("memory_count", 0)),
                impact_summary=state.get("impact_summary", []),
                verify_dropped=int(state.get("verify_dropped", 0)),
                verify_downgraded=int(state.get("verify_downgraded", 0)),
            )

        if dry_run:
            return {"report": render(), "posted": False}
        if not reviews and state.get("errors"):
            # Every file failed: do not post a misleading "no issues" comment.
            return {"report": render(), "posted": False, "errors": ["所有文件的审查都失败，未发布评论。"]}
        if not reviews and not state.get("files"):
            return {"report": render(), "posted": False}  # nothing reviewable in this change
        try:
            if target.pr_number is not None:
                me = github.authenticated_login()
                fingerprints, anchors = existing_fingerprints(
                    github.list_pull_request_review_comments(target.repo, target.pr_number), me
                )
                inline_all = build_inline_comments(reviews)

                def already(comment: dict[str, Any]) -> bool:
                    return fingerprint_from_body(comment["body"]) in fingerprints or (comment["path"], comment["line"]) in anchors

                fresh = [comment for comment in inline_all if not already(comment)]
                posted_ok = True
                if fresh:
                    posted_ok = github.post_pull_request_review(
                        target.repo, target.pr_number, target.sha, f"自动代码审查：新增 {len(fresh)} 条行内评论，汇总见 PR 评论。", fresh
                    )
                keys = {(c["path"], c["line"]) for c in inline_all if already(c) or (posted_ok and c in fresh)}
                github.upsert_issue_comment(target.repo, target.pr_number, render(keys or None), MARKER)
                return {"report": render(keys or None), "posted": True}
            report = render()
            github.upsert_commit_comment(target.repo, target.sha, report, MARKER)
            return {"report": report, "posted": True}
        except GitHubError as error:
            logger.error("Posting the review failed: %s", error)
            return {"report": render(), "posted": False, "errors": [f"发布评论失败：{error}"]}

    def learn(state: ReviewState) -> dict[str, Any]:
        if memory is None or dry_run:
            return {}
        target = ReviewTarget.model_validate(state["target"])
        try:
            retained = memory.learn(github, target.repo)
            if retained:
                logger.info("Retained %d memory item(s) for %s", retained, target.repo)
        except Exception as error:  # memory is best-effort and must never fail a review
            logger.warning("Learning step failed: %s", type(error).__name__)
        return {}

    builder = StateGraph(ReviewState)
    builder.add_node("fetch_changes", fetch_changes)
    builder.add_node("triage", triage)
    builder.add_node("impact_analysis", impact_analysis)
    builder.add_node("recall_memory", recall_memory)
    builder.add_node("review_file", review_file)
    builder.add_node("verify", verify)
    builder.add_node("synthesize", synthesize)
    builder.add_node("publish", publish)
    builder.add_node("learn", learn)
    builder.add_edge(START, "fetch_changes")
    builder.add_edge("fetch_changes", "triage")
    builder.add_edge("triage", "impact_analysis")
    builder.add_edge("impact_analysis", "recall_memory")
    builder.add_conditional_edges("recall_memory", dispatch, ["review_file", "verify"])
    builder.add_edge("review_file", "verify")
    builder.add_edge("verify", "synthesize")
    builder.add_edge("synthesize", "publish")
    builder.add_edge("publish", "learn")
    builder.add_edge("learn", END)
    return builder.compile()


def run_review(graph: Any, target: ReviewTarget, settings: Settings) -> dict[str, Any]:
    return graph.invoke({"target": target.model_dump()}, config={"max_concurrency": settings.llm_concurrency})
