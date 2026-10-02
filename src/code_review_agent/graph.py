"""The LangGraph review workflow.

fetch_changes -> triage -> (fan out: one review_file per file, in parallel) -> synthesize -> publish

* fetch_changes  : read the commit / pull request files from GitHub
* triage         : drop generated/binary/oversized files and cap the amount of work
* review_file    : DeepSeek reviews one file (chunked); findings are validated against the real diff
* synthesize     : merge/sort findings, derive the verdict deterministically, ask DeepSeek for a short summary
* publish        : post a commit comment (push) or a PR review with inline comments (unless dry-run)
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .config import Settings
from .diff_utils import annotate_patch, chunk_text, should_review
from .github_client import GitHubClient, GitHubError
from .llm import extract_json
from .models import ChangedFile, FileReview, Finding, ReviewTarget
from .prompts import FILE_REVIEW_SYSTEM, FILE_REVIEW_USER, SUMMARY_SYSTEM, SUMMARY_USER
from .report import build_inline_comments, collect, render_report, verdict_for

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_CHUNK = 8
MAX_FINDINGS_PER_FILE = 12
MAX_MESSAGE_CHARS = 2000


class ReviewState(TypedDict, total=False):
    target: dict[str, Any]
    commit_message: str
    files: list[dict[str, Any]]
    skipped: list[str]
    # Parallel branches append to these lists; the reducer merges them.
    file_reviews: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]
    verdict: str
    summary: str
    report: str
    posted: bool


class FileTask(TypedDict):
    file: dict[str, Any]
    commit_message: str


def _parse_findings(data: dict[str, Any] | None) -> list[Finding]:
    findings: list[Finding] = []
    raw_items = data.get("findings") if data else None
    if not isinstance(raw_items, list):
        return findings
    for item in raw_items[:MAX_FINDINGS_PER_CHUNK]:
        try:
            findings.append(Finding.model_validate(item))
        except Exception:  # one malformed finding must not discard the rest
            continue
    return findings


def build_graph(settings: Settings, github: GitHubClient, llm: BaseChatModel, *, dry_run: bool = False):
    """Compile the review graph. Dependencies are injected so tests can use fakes."""

    def fetch_changes(state: ReviewState) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        if target.pr_number is not None:
            files = github.get_pull_request_files(target.repo, target.pr_number)
            message = github.get_pull_request_title(target.repo, target.pr_number)
        else:
            message, files = github.get_commit(target.repo, target.sha)
        return {"commit_message": message[:MAX_MESSAGE_CHARS], "files": [file.model_dump() for file in files]}

    def triage(state: ReviewState) -> dict[str, Any]:
        selected: list[dict[str, Any]] = []
        skipped: list[str] = []
        for raw in state.get("files", []):
            file = ChangedFile.model_validate(raw)
            if file.status == "removed":
                skipped.append(f"{file.filename}: 已删除")
            elif not should_review(file.filename):
                skipped.append(f"{file.filename}: 生成/二进制/依赖文件")
            elif not file.patch:
                skipped.append(f"{file.filename}: 无 diff（二进制或过大）")
            elif len(selected) >= settings.max_files_per_review:
                skipped.append(f"{file.filename}: 超过单次审查文件上限")
            else:
                selected.append(file.model_dump())
        return {"files": selected, "skipped": skipped}

    def dispatch(state: ReviewState) -> list[Send] | str:
        files = state.get("files", [])
        if not files:
            return "synthesize"
        message = state.get("commit_message", "")
        return [Send("review_file", {"file": file, "commit_message": message}) for file in files]

    def review_file(task: FileTask) -> dict[str, Any]:
        file = ChangedFile.model_validate(task["file"])
        patch = file.patch or ""
        truncated = len(patch) > settings.max_patch_chars_per_file
        annotated, valid_lines = annotate_patch(patch[: settings.max_patch_chars_per_file])
        chunks = chunk_text(annotated, settings.max_chunk_chars)
        findings: list[Finding] = []
        errors: list[str] = []
        system = FILE_REVIEW_SYSTEM.format(language=settings.review_language)
        for index, chunk in enumerate(chunks, start=1):
            user = FILE_REVIEW_USER.format(
                filename=file.filename,
                index=index,
                total=len(chunks),
                truncated=" (diff truncated: only the first part is shown)" if truncated else "",
                message=task.get("commit_message", ""),
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
            findings.extend(_parse_findings(parsed))

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
        # A file whose every chunk failed is not "reviewed"; it is only reported through errors.
        if not errors or len(errors) < len(chunks):
            update["file_reviews"] = [review.model_dump()]
        return update

    def synthesize(state: ReviewState) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        reviews = [FileReview.model_validate(item) for item in state.get("file_reviews", [])]
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
        inline = build_inline_comments(reviews) if target.pr_number is not None else []
        inline_keys = {(item["path"], item["line"]) for item in inline}
        common = dict(
            sha=target.sha,
            verdict=verdict,
            summary=summary,
            file_reviews=reviews,
            skipped=state.get("skipped", []),
            errors=state.get("errors", []),
            model=settings.deepseek_model,
        )
        report = render_report(**common, inline_paths=inline_keys or None)
        return {"verdict": verdict, "summary": summary, "report": report}

    def publish(state: ReviewState) -> dict[str, Any]:
        target = ReviewTarget.model_validate(state["target"])
        reviews = [FileReview.model_validate(item) for item in state.get("file_reviews", [])]
        if dry_run:
            return {"posted": False}
        if not reviews and state.get("errors"):
            # Every file failed: do not post a misleading "no issues" comment.
            return {"posted": False, "errors": ["所有文件的审查都失败，未发布评论。"]}
        if not reviews and not state.get("files"):
            return {"posted": False}  # nothing reviewable in this change
        try:
            if target.pr_number is not None:
                inline = build_inline_comments(reviews)
                fallback = render_report(
                    sha=target.sha, verdict=state["verdict"], summary=state.get("summary", ""), file_reviews=reviews,
                    skipped=state.get("skipped", []), errors=state.get("errors", []), model=settings.deepseek_model,
                )
                github.post_pull_request_review(target.repo, target.pr_number, target.sha, state["report"], inline, fallback)
            else:
                github.post_commit_comment(target.repo, target.sha, state["report"])
        except GitHubError as error:
            logger.error("Posting the review failed: %s", error)
            return {"posted": False, "errors": [f"发布评论失败：{error}"]}
        return {"posted": True}

    builder = StateGraph(ReviewState)
    builder.add_node("fetch_changes", fetch_changes)
    builder.add_node("triage", triage)
    builder.add_node("review_file", review_file)
    builder.add_node("synthesize", synthesize)
    builder.add_node("publish", publish)
    builder.add_edge(START, "fetch_changes")
    builder.add_edge("fetch_changes", "triage")
    builder.add_conditional_edges("triage", dispatch, ["review_file", "synthesize"])
    builder.add_edge("review_file", "synthesize")
    builder.add_edge("synthesize", "publish")
    builder.add_edge("publish", END)
    return builder.compile()


def run_review(graph: Any, target: ReviewTarget, settings: Settings) -> dict[str, Any]:
    return graph.invoke({"target": target.model_dump()}, config={"max_concurrency": settings.llm_concurrency})
