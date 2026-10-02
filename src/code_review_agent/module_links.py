"""Does file A depend on file B? Resolve an import to a repository path with plain string rules (no filesystem).

Name matching alone cannot tell the ``render`` of one module from the ``render`` of another. An import that points at
the changed file is positive evidence that a use really refers to the changed symbol.

The rules are deliberately conservative heuristics, not a resolver: source roots, path aliases (``@/``), namespace
packages and re-exports are not modelled. A False result means "no evidence", never "unrelated".
"""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath

from .code_index import ImportRef, family

_JS_SUFFIXES = (".d.ts", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".mts", ".cts")


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./") if path.startswith("./") else path.replace("\\", "/")


def python_modules(path: str) -> set[str]:
    """Every dotted name a Python file may be imported by (source roots such as ``src/`` are unknown, so all suffixes)."""
    pure = PurePosixPath(_norm(path))
    parts = list(pure.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[i:]) for i in range(len(parts))}


def _python_links(importer: str, ref: ImportRef, changed: str) -> bool:
    target = _norm(changed)
    if ref.level:
        base = PurePosixPath(_norm(importer)).parent
        for _ in range(ref.level - 1):
            base = base.parent
        joined = base.joinpath(*ref.spec.split(".")) if ref.spec else base
        candidates = {f"{joined}.py", f"{joined}.pyi", f"{joined}/__init__.py"}
        candidates |= {f"{joined}/{name}.py" for name in ref.names if name != "*"} | {f"{joined}/{name}/__init__.py" for name in ref.names if name != "*"}
        return target in {c.replace("\\", "/") for c in candidates}
    forms = python_modules(changed)
    names = {ref.spec} | {f"{ref.spec}.{name}" if ref.spec else name for name in ref.names if name != "*"}
    return bool(names & forms) or (ref.spec == "" and bool(forms & set(ref.names)))


def _js_stem(path: str) -> str:
    path = _norm(path)
    for suffix in _JS_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return path[: -len("/index")] if path.endswith("/index") else path


def _js_links(importer: str, ref: ImportRef, changed: str) -> bool:
    spec = ref.spec.strip()
    if not spec:
        return False
    target = _js_stem(changed)
    if spec.startswith("."):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(_norm(importer)), spec))
        return _js_stem(resolved) == target
    for alias in ("@/", "~/", "#/"):
        if spec.startswith(alias):
            spec = spec[len(alias):]
            break
    spec = _js_stem(spec)
    return bool(spec) and "/" in spec and (target == spec or target.endswith("/" + spec))


def _java_links(importer: str, ref: ImportRef, changed: str) -> bool:
    dotted = ".".join(PurePosixPath(_norm(changed)).with_suffix("").parts)
    package = ".".join(PurePosixPath(_norm(changed)).parent.parts)
    spec = ref.spec
    if ref.wildcard:
        return package == spec or package.endswith("." + spec)
    candidates = {spec}
    if ref.static and "." in spec:
        candidates.add(spec.rsplit(".", 1)[0])  # import static a.b.C.member -> a.b.C
    return any(dotted == c or dotted.endswith("." + c) for c in candidates)


def imports_link(importer: str, imports: list[ImportRef], changed: str, language: str) -> bool:
    """True when any import in ``importer`` points at the file ``changed`` (paths are repository-relative, POSIX)."""
    fam = family(language)
    check = {"python": _python_links, "java": _java_links}.get(fam, _js_links)
    return any(check(importer, ref, changed) for ref in imports)


def same_java_package(importer: str, changed: str) -> bool:
    """Java needs no import between classes of one package."""
    return posixpath.dirname(_norm(importer)) == posixpath.dirname(_norm(changed))
