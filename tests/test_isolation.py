"""Process isolation: a crashing or hanging worker must not take the caller down."""

import os
import time

import pytest

from code_review_agent.config import Settings
from code_review_agent.impact import analyze_impact
from code_review_agent.isolation import IsolationError, run_isolated
from code_review_agent.models import ChangedFile


def add(a, b):
    return a + b


def boom():
    raise ValueError("secret repository text must not leak")


def hard_crash():
    os._exit(3)  # what a native access violation looks like to the parent


def hang():
    time.sleep(60)


def test_result_is_returned_from_the_worker():
    assert run_isolated(add, (2, 3), timeout=60) == 5


def test_exceptions_are_reported_by_type_only():
    with pytest.raises(IsolationError) as error:
        run_isolated(boom, (), timeout=60)
    assert "ValueError" in str(error.value) and "secret repository text" not in str(error.value)


def test_a_crashing_worker_is_contained():
    with pytest.raises(IsolationError, match="crashed"):
        run_isolated(hard_crash, (), timeout=60)


def test_a_hanging_worker_is_killed_after_the_timeout():
    started = time.monotonic()
    with pytest.raises(IsolationError, match="did not finish"):
        run_isolated(hang, (), timeout=3)
    assert time.monotonic() - started < 30


def test_impact_analysis_runs_in_a_worker_process(tmp_path):
    (tmp_path / "lib.py").write_text("def compute(a, b):\n    return a\n", encoding="utf-8")
    (tmp_path / "use.py").write_text("from lib import compute\n\n\ndef go():\n    return compute(1)\n", encoding="utf-8")
    patch = "@@ -1,2 +1,2 @@\n-def compute(a):\n+def compute(a, b):\n     return a\n"
    report = run_isolated(analyze_impact, (tmp_path, [ChangedFile(filename="lib.py", patch=patch)], {"lib.py"}, Settings(deepseek_api_key="k")), 120)
    assert "use.py:5 in go()" in report.by_file["lib.py"]
