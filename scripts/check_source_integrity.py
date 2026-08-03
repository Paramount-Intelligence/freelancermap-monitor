"""Static source-integrity audit for CI.

Verifies four properties across the repository:

1. Every Python file parses cleanly (syntax check).
2. No module shadows a top-level ``def`` or ``class`` name (silent redefinition).
3. Every import resolves to the standard library, a dependency declared in
   ``requirements.txt``, or a local project module.
4. Unit tests never construct a real browser driver directly (forbidden
   ``webdriver.Chrome()`` style calls).

Exits 0 on success and 1 when any finding is reported.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "data"}
FORBIDDEN_DRIVERS = {"chrome", "firefox", "edge", "safari"}
DISTRIBUTION_IMPORT_NAMES = {
    "beautifulsoup4": "bs4",
    "python-dotenv": "dotenv",
}


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts)
    )


def _declared_dependencies(root: pathlib.Path) -> set[str]:
    requirements = root / "requirements.txt"
    if not requirements.exists():
        return set()
    declared: set[str] = set()
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        name = re.split(r"[<>=!~;[]", raw.strip(), maxsplit=1)[0].strip()
        if name:
            declared.add(DISTRIBUTION_IMPORT_NAMES.get(name, name.lower()))
    return declared


def _local_modules(root: pathlib.Path) -> set[str]:
    return {path.stem for path in root.glob("*.py")}


def _import_root(node: ast.Import | ast.ImportFrom) -> str:
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0]
    if node.module:
        return node.module.split(".")[0]
    return ""


def _driver_call_receiver_contains_webdriver(node: ast.AST) -> bool:
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return isinstance(current, ast.Name) and current.id == "webdriver"


def _check_syntax(files: list[pathlib.Path]) -> tuple[list[str], dict[pathlib.Path, ast.AST]]:
    findings: list[str] = []
    trees: dict[pathlib.Path, ast.AST] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"{path}: could not decode as UTF-8")
            continue
        try:
            trees[path] = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            findings.append(f"{path}:{exc.lineno}: syntax error: {exc.msg}")
    return findings, trees


def _check_duplicate_definitions(
    trees: dict[pathlib.Path, ast.AST],
) -> list[str]:
    findings: list[str] = []
    for path, tree in trees.items():
        counter: Counter[str] = Counter()
        locations: dict[str, list[int]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                counter[node.name] += 1
                locations.setdefault(node.name, []).append(node.lineno)
        for name, count in counter.items():
            if count > 1:
                lines = ", ".join(str(line) for line in locations[name])
                findings.append(
                    f"{path}: top-level {name!r} defined {count} times (lines {lines})"
                )
    return findings


def _check_imports(
    trees: dict[pathlib.Path, ast.AST],
    stdlib: set[str],
    declared: set[str],
    local: set[str],
) -> list[str]:
    findings: list[str] = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            root = _import_root(node)
            if not root or root == "__future__":
                continue
            if root in stdlib or root in declared or root in local:
                continue
            findings.append(
                f"{path}:{node.lineno}: import {root!r} is not stdlib, not "
                "declared in requirements.txt, and not a local module"
            )
    return findings


def _check_test_driver_construction(trees: dict[pathlib.Path, ast.AST]) -> list[str]:
    findings: list[str] = []
    for path, tree in trees.items():
        if "tests" not in path.parts:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr.casefold() not in FORBIDDEN_DRIVERS:
                continue
            if _driver_call_receiver_contains_webdriver(func):
                findings.append(
                    f"{path}:{node.lineno}: direct webdriver.{func.attr}() "
                    "construction is forbidden in unit tests"
                )
    return findings


def main() -> int:
    files = _python_files(ROOT)
    if not files:
        print("No Python files found to audit.")
        return 1

    findings: list[str] = []
    syntax_findings, trees = _check_syntax(files)
    findings += syntax_findings
    findings += _check_duplicate_definitions(trees)
    findings += _check_imports(
        trees,
        stdlib=set(sys.stdlib_module_names),
        declared=_declared_dependencies(ROOT),
        local=_local_modules(ROOT),
    )
    findings += _check_test_driver_construction(trees)

    print(f"Audited {len(files)} Python files.")
    if not findings:
        print("Source integrity audit passed.")
        return 0
    for finding in findings:
        print(f"FAIL: {finding}")
    print(f"Source integrity audit failed with {len(findings)} finding(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
