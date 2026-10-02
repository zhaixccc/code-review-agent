"""Command line: run the webhook server or review a single commit / pull request locally."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

from .cache import build_cache
from .config import load_settings
from .github_client import GitHubClient, GitHubError, validate_repo, validate_sha
from .graph import build_graph, run_review
from .llm import make_llm
from .memory import build_memory
from .models import ReviewTarget
from .snapshot import build_snapshots
from .state import StateStore


def _review(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.require_llm()
    if args.post:
        settings.require_github_write()
    if args.no_impact:
        settings = dataclasses.replace(settings, impact_enabled=False)
    if args.no_verify:
        settings = dataclasses.replace(settings, verify_findings=False)
    target = ReviewTarget(repo=validate_repo(args.repo), sha=validate_sha(args.sha), pr_number=args.pr)
    github = GitHubClient(settings.github_token, settings.github_api_url)
    memory = None
    try:
        memory = None if args.no_memory else build_memory(settings, StateStore(settings.state_dir))
        graph = build_graph(
            settings, github, make_llm(settings), dry_run=not args.post, memory=memory,
            snapshots=build_snapshots(settings, github), cache=build_cache(settings),
        )
        result = run_review(graph, target, settings)
    finally:
        github.close()
        if memory is not None:
            memory.close()
    print(result.get("report") or "(没有可审查的改动)")
    for error in result.get("errors", []):
        print(f"[error] {error}", file=sys.stderr)
    if args.explain:
        for path, text in (result.get("impact") or {}).items():
            print(f"\n[impact evidence] {path}\n{text}", file=sys.stderr)
        for entry in result.get("verify_log") or []:
            print(f"[verify] {entry['verdict']}: {entry['file']} | {entry['title']} | {entry['reason']}", file=sys.stderr)
    if args.post:
        print("已发布到 GitHub。" if result.get("posted") else "未发布评论。", file=sys.stderr)
    return 1 if result.get("errors") and not result.get("file_reviews") else 0


def _eval(args: argparse.Namespace) -> int:
    from .evaluation import CASES, run_eval, save_report

    if args.list:
        for case in CASES:
            print(f"{case.name:<26} {case.category:<10} {case.description}")
        return 0
    settings = load_settings()
    settings.require_llm()
    if args.no_impact:
        settings = dataclasses.replace(settings, impact_enabled=False)
    if args.no_verify:
        settings = dataclasses.replace(settings, verify_findings=False)
    report = run_eval(settings, make_llm(settings), args.case or None, args.runs)
    print(report.render())
    print(f"\n详细结果已保存：{save_report(report, settings.state_dir / 'evals')}", file=sys.stderr)
    return 0


def _require_memory(settings):
    memory = build_memory(settings, StateStore(settings.state_dir))
    if memory is None:
        raise RuntimeError("HINDSIGHT_URL is not set; long-term memory is disabled.")
    return memory


def _learn(args: argparse.Namespace) -> int:
    settings = load_settings()
    settings.require_github_write()
    memory = _require_memory(settings)
    github = GitHubClient(settings.github_token, settings.github_api_url)
    try:
        repo = validate_repo(args.repo)
        conventions = memory.learn_conventions(github, repo)
        feedback = memory.learn_feedback(github, repo) if not args.skip_feedback else 0
    finally:
        github.close()
        memory.close()
    print(f"已写入记忆：规范文档 {conventions} 份，维护者反馈 {feedback} 条。")
    return 0


def _recall(args: argparse.Namespace) -> int:
    settings = load_settings()
    memory = _require_memory(settings)
    context = memory.recall_for_review(validate_repo(args.repo), args.path or ["src/example.py"], args.query or "")
    memory.close()
    print(context.text or "(没有召回到记忆，或记忆服务不可用)")
    return 0


def _serve(_: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    settings.require_webhook()
    uvicorn.run("code_review_agent.server:app_factory", factory=True, host=settings.host, port=settings.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "langchain_openai", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="code-review-agent")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="review one commit or pull request (dry-run unless --post)")
    review.add_argument("--repo", required=True, help="owner/name")
    review.add_argument("--sha", required=True, help="commit sha (for a PR: the head commit)")
    review.add_argument("--pr", type=int, default=None, help="pull request number")
    review.add_argument("--post", action="store_true", help="post the review to GitHub")
    review.add_argument("--no-memory", action="store_true", help="do not use Hindsight memory for this run")
    review.add_argument("--no-impact", action="store_true", help="skip repository-wide impact analysis (tree-sitter)")
    review.add_argument("--no-verify", action="store_true", help="skip the second-pass verification of findings")
    review.add_argument("--explain", action="store_true", help="print impact evidence and verification decisions to stderr")
    review.set_defaults(handler=_review)

    learn = commands.add_parser("learn", help="retain repository conventions and maintainer feedback into Hindsight")
    learn.add_argument("--repo", required=True, help="owner/name")
    learn.add_argument("--skip-feedback", action="store_true", help="only ingest convention documents")
    learn.set_defaults(handler=_learn)

    recall = commands.add_parser("recall", help="show what memory would be given to a review")
    recall.add_argument("--repo", required=True, help="owner/name")
    recall.add_argument("--path", action="append", help="changed file path (repeatable)")
    recall.add_argument("--query", default="", help="extra text, e.g. a commit headline")
    recall.set_defaults(handler=_recall)

    serve = commands.add_parser("serve", help="run the GitHub webhook server")
    serve.set_defaults(handler=_serve)

    evaluate = commands.add_parser("eval", help="score the agent on seeded-defect cases (uses the DeepSeek API)")
    evaluate.add_argument("--case", action="append", help="run only this case (repeatable)")
    evaluate.add_argument("--list", action="store_true", help="list the available cases")
    evaluate.add_argument("--runs", type=int, default=1, help="repeat every case N times to see run-to-run variance")
    evaluate.add_argument("--no-impact", action="store_true", help="disable impact analysis (for A/B comparison)")
    evaluate.add_argument("--no-verify", action="store_true", help="disable second-pass verification (for A/B comparison)")
    evaluate.set_defaults(handler=_eval)

    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (RuntimeError, ValueError, GitHubError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
