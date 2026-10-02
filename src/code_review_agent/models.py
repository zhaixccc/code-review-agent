"""Data models shared by the graph, GitHub client and formatter."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "major", "minor", "nit"]
Category = Literal["bug", "security", "performance", "maintainability", "testing", "style"]
Verdict = Literal["approve", "comment", "request_changes"]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "major": 1, "minor": 2, "nit": 3}


class ChangedFile(BaseModel):
    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None


class Finding(BaseModel):
    severity: Severity
    category: Category
    line: Optional[int] = Field(default=None, ge=1)
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=2000)
    suggestion: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("severity", "category", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class FileReviewResult(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


class FileReview(BaseModel):
    filename: str
    findings: list[Finding] = Field(default_factory=list)
    valid_lines: list[int] = Field(default_factory=list)
    truncated: bool = False


class Summary(BaseModel):
    summary: str = Field(default="", max_length=1500)
    verdict: Verdict = "comment"


class ReviewTarget(BaseModel):
    """What to review: a single commit or a pull request head."""

    repo: str
    sha: str
    pr_number: Optional[int] = None
