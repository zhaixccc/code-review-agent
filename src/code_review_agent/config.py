"""Environment-based configuration. Secrets are only ever read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    return max(minimum, value)


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str = field(default="", repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    github_token: str = field(default="", repr=False)
    github_webhook_secret: str = field(default="", repr=False)
    github_api_url: str = "https://api.github.com"
    allowed_repos: tuple[str, ...] = ()
    review_events: tuple[str, ...] = ("push",)
    max_commits_per_push: int = 5
    max_files_per_review: int = 30
    max_patch_chars_per_file: int = 24_000
    max_chunk_chars: int = 12_000
    max_total_patch_chars: int = 160_000
    max_commit_pages: int = 5
    llm_concurrency: int = 4
    review_language: str = "Simplified Chinese"
    state_dir: Path = _PROJECT_ROOT / ".state"
    # Hindsight long-term memory (disabled when hindsight_url is empty)
    hindsight_url: str = ""
    hindsight_api_key: str = field(default="", repr=False)
    memory_recall_max_tokens: int = 1500
    memory_timeout_seconds: int = 20
    memory_conventions: tuple[str, ...] = ("AGENTS.md", "CONTRIBUTING.md", ".github/copilot-instructions.md", "docs/CONTRIBUTING.md")
    memory_feedback_ttl_minutes: int = 30
    trusted_feedback_users: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def memory_enabled(self) -> bool:
        return bool(self.hindsight_url)

    def require_llm(self) -> None:
        if not self.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Copy .env.example to .env and fill it in.")

    def require_github_write(self) -> None:
        if not self.github_token:
            raise RuntimeError("GITHUB_TOKEN is not set; it is required to post review comments.")

    def require_webhook(self) -> None:
        self.require_llm()
        self.require_github_write()
        if len(self.github_webhook_secret) < 16:
            raise RuntimeError("GITHUB_WEBHOOK_SECRET must be set to a random string of at least 16 characters.")

    def is_repo_allowed(self, full_name: str) -> bool:
        if not self.allowed_repos:
            return True
        return full_name.lower() in {repo.lower() for repo in self.allowed_repos}


def load_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env")
    return Settings(
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"),
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip(),
        github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/"),
        allowed_repos=_csv("ALLOWED_REPOS"),
        review_events=_csv("REVIEW_EVENTS", "push") or ("push",),
        max_commits_per_push=_int("MAX_COMMITS_PER_PUSH", 5),
        max_files_per_review=_int("MAX_FILES_PER_REVIEW", 30),
        max_patch_chars_per_file=_int("MAX_PATCH_CHARS_PER_FILE", 24_000, 1000),
        max_chunk_chars=_int("MAX_CHUNK_CHARS", 12_000, 1000),
        max_total_patch_chars=_int("MAX_TOTAL_PATCH_CHARS", 160_000, 5000),
        max_commit_pages=_int("MAX_COMMIT_PAGES", 5),
        llm_concurrency=_int("LLM_CONCURRENCY", 4),
        review_language=os.environ.get("REVIEW_LANGUAGE", "Simplified Chinese").strip()[:40] or "Simplified Chinese",
        state_dir=Path(os.environ.get("STATE_DIR", "").strip() or _PROJECT_ROOT / ".state"),
        hindsight_url=os.environ.get("HINDSIGHT_URL", "").strip().rstrip("/"),
        hindsight_api_key=os.environ.get("HINDSIGHT_API_KEY", "").strip(),
        memory_recall_max_tokens=_int("MEMORY_RECALL_MAX_TOKENS", 1500, 200),
        memory_timeout_seconds=_int("MEMORY_TIMEOUT_SECONDS", 20),
        memory_conventions=_csv("MEMORY_CONVENTION_FILES", "AGENTS.md,CONTRIBUTING.md,.github/copilot-instructions.md,docs/CONTRIBUTING.md"),
        memory_feedback_ttl_minutes=_int("MEMORY_FEEDBACK_TTL_MINUTES", 30),
        trusted_feedback_users=_csv("TRUSTED_FEEDBACK_USERS"),
        host=os.environ.get("HOST", "127.0.0.1").strip(),
        port=_int("PORT", 8080),
    )
