"""Import extraction and import-to-file resolution (the signal that tells same-named symbols apart)."""

import pytest

from code_review_agent.code_index import ImportRef, ParsedFile
from code_review_agent.module_links import imports_link, python_modules, same_java_package


def imports(source: str, language: str) -> list[ImportRef]:
    return ParsedFile.open(source.encode(), language).imports()


# ------------------------------------------------------------------------------ extraction ---
def test_python_imports_are_decoded():
    found = imports(
        "import os, a.b as ab\n"
        "from billing.pricing import apply_discount as ad, other\n"
        "from . import views\n"
        "from ..core.utils import (\n    helper,\n)\n"
        "from x import *\n"
        "def f():\n    import lazy.mod\n",
        "python",
    )
    by_spec = {(r.spec, r.level, r.names) for r in found}
    assert ("os", 0, ()) in by_spec and ("a.b", 0, ()) in by_spec
    assert ("billing.pricing", 0, ("apply_discount", "other")) in by_spec
    assert ("", 1, ("views",)) in by_spec
    assert ("core.utils", 2, ("helper",)) in by_spec
    assert ("x", 0, ("*",)) in by_spec
    assert ("lazy.mod", 0, ()) in by_spec  # imports inside functions count too


def test_js_ts_imports_exports_and_requires_are_decoded():
    found = imports(
        "import { a } from './format';\nimport def from \"@/utils/x\";\nexport * from './reexport';\n"
        "const m = require('../lib/old');\nconst n = other('./notarequire');\n",
        "typescript",
    )
    assert sorted(r.spec for r in found) == ["./format", "./reexport", "../lib/old", "@/utils/x"] or {r.spec for r in found} == {
        "./format", "@/utils/x", "./reexport", "../lib/old",
    }


def test_java_imports_are_decoded():
    found = imports("package p;\nimport com.x.Util;\nimport static com.x.Helper.help;\nimport java.util.*;\nclass A {}\n", "java")
    by_spec = {r.spec: r for r in found}
    assert by_spec["com.x.Util"].wildcard is False and by_spec["com.x.Helper.help"].static is True
    assert by_spec["java.util"].wildcard is True


# ------------------------------------------------------------------------------ resolution ---
def link(importer, source, changed, language):
    return imports_link(importer, imports(source, language), changed, language)


@pytest.mark.parametrize(
    "importer, source, changed, expected",
    [
        ("orders/checkout.py", "from billing.pricing import apply_discount\n", "billing/pricing.py", True),
        ("orders/checkout.py", "import billing.pricing as p\n", "billing/pricing.py", True),
        ("orders/checkout.py", "from billing import pricing\n", "billing/pricing.py", True),
        ("orders/checkout.py", "from src.billing.pricing import x\n", "src/billing/pricing.py", True),
        ("orders/checkout.py", "from billing import pricing\n", "billing/__init__.py", True),  # the package itself
        ("orders/checkout.py", "from legacy.pricing import apply_discount\n", "billing/pricing.py", False),
        ("orders/checkout.py", "from billing.tax import apply_discount\n", "billing/pricing.py", False),
        ("billing/checkout.py", "from .pricing import apply_discount\n", "billing/pricing.py", True),
        ("billing/checkout.py", "from . import pricing\n", "billing/pricing.py", True),
        ("billing/sub/checkout.py", "from ..pricing import apply_discount\n", "billing/pricing.py", True),
        ("billing/sub/checkout.py", "from .pricing import apply_discount\n", "billing/pricing.py", False),
        ("a/b.py", "import os\n", "billing/pricing.py", False),
    ],
)
def test_python_import_resolution(importer, source, changed, expected):
    assert link(importer, source, changed, "python") is expected


@pytest.mark.parametrize(
    "importer, source, changed, expected",
    [
        ("src/cart.ts", "import { formatPrice } from './format';\n", "src/format.ts", True),
        ("src/ui/cart.ts", "import { formatPrice } from '../format';\n", "src/format.ts", True),
        ("src/cart.ts", "import { x } from './format.js';\n", "src/format.ts", True),  # TS ESM names the emitted .js
        ("src/cart.ts", "import { x } from './lib';\n", "src/lib/index.ts", True),
        ("src/cart.ts", "import { x } from '@/format';\n", "src/format.ts", False),  # a bare alias has no "/" anchor
        ("src/cart.ts", "import { x } from '@/utils/format';\n", "src/utils/format.ts", True),
        ("src/cart.ts", "import { x } from './other';\n", "src/format.ts", False),
        ("src/cart.ts", "import React from 'react';\n", "src/format.ts", False),
        ("lib/a.js", "const f = require('./b');\n", "lib/b.js", True),
        ("lib/a.js", "export { f } from './b';\n", "lib/b.js", True),
    ],
)
def test_js_import_resolution(importer, source, changed, expected):
    assert link(importer, source, changed, "typescript") is expected


@pytest.mark.parametrize(
    "importer, source, changed, expected",
    [
        ("src/main/java/org/app/Main.java", "import com.x.Util;\n", "src/main/java/com/x/Util.java", True),
        ("src/main/java/org/app/Main.java", "import com.x.*;\n", "src/main/java/com/x/Util.java", True),
        ("src/main/java/org/app/Main.java", "import static com.x.Util.help;\n", "src/main/java/com/x/Util.java", True),
        ("src/main/java/org/app/Main.java", "import com.y.Util;\n", "src/main/java/com/x/Util.java", False),
        ("src/main/java/org/app/Main.java", "import java.util.List;\n", "src/main/java/com/x/Util.java", False),
    ],
)
def test_java_import_resolution(importer, source, changed, expected):
    assert link(importer, source, changed, "java") is expected


def test_java_same_package_needs_no_import():
    assert same_java_package("src/com/x/A.java", "src/com/x/B.java") is True
    assert same_java_package("src/com/y/A.java", "src/com/x/B.java") is False


def test_python_module_forms_cover_every_source_root():
    assert python_modules("src/billing/pricing.py") == {"src.billing.pricing", "billing.pricing", "pricing"}
    assert python_modules("billing/__init__.py") == {"billing"}
