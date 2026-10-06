import pytest

from code_review_agent.diff_utils import annotated_added_lines, annotate_patch, chunk_text, should_review

PATCH = """@@ -1,4 +1,6 @@
 import os
-old = 1
+new = 2
+extra = 3
 keep = 4
 last = 5
@@ -20,2 +22,3 @@ def f():
     return 1
+    print("x")
"""


def test_annotate_patch_numbers_added_and_context_lines():
    text, added = annotate_patch(PATCH)
    assert added == {2, 3, 23}
    assert "    2 + new = 2" in text
    assert "      - old = 1" in text
    assert "    1   import os" in text
    assert "   23 +     print(\"x\")" in text


def test_annotate_patch_ignores_no_newline_marker():
    text, added = annotate_patch("@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n")
    assert added == {1}
    assert "No newline" not in text


def test_annotated_added_lines_extracts_only_added_line_anchors():
    annotated, _ = annotate_patch(PATCH)
    assert annotated_added_lines(annotated) == {2, 3, 23}


def test_annotated_added_lines_handles_empty_and_non_added_rows():
    assert annotated_added_lines("") == set()
    assert annotated_added_lines("      - removed\n    3   context only\n") == set()


def test_annotated_added_lines_does_not_confuse_code_text_with_the_anchor():
    annotated = "    5 + 123 + this is code\n    6   123 + this is context\n"
    assert annotated_added_lines(annotated) == {5}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("src/app.py", True),
        ("package-lock.json", False),
        ("web/dist/bundle.js", False),
        ("static/app.min.js", False),
        ("logo.PNG", False),
        ("node_modules/x/index.js", False),
        ("build/agentHost/generateMetadata.ts", True),
        ("docs/readme.md", True),
    ],
)
def test_should_review(name, expected):
    assert should_review(name) is expected


def test_chunk_text_respects_limit_and_keeps_all_lines():
    text = "\n".join(f"line {i}" for i in range(200))
    chunks = chunk_text(text, 300)
    assert all(len(chunk) <= 300 for chunk in chunks)
    assert "\n".join(chunks).count("line ") == 200
    assert chunk_text("", 100) == []
    assert chunk_text("short", 100) == ["short"]
