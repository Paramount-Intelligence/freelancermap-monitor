"""Static source-integrity audit for CI.

Verifies four properties across the repository:

1. Every Python file parses cleanly (syntax check).
2. No module shadows a top-level ``def`` or ``class`` name (silent redefinition).
3. Every import resolves to the standard library, a dependency declared in
   ``requirements.txt``, or a local project module. Every alias in a
   multi-alias ``import alpha, beta, gamma`` statement is validated
   individually, and local packages (directories with ``__init__.py``) are
   resolvable just like top-level modules.
4. Unit tests never construct a real browser driver directly. Detected
   forms: ``webdriver.Chrome()`` style attribute calls (including aliased
   receivers such as ``wd.Chrome()``), and direct class construction from
   selenium imports such as ``from selenium.webdriver import Chrome`` or
   ``from selenium.webdriver.chrome.webdriver import WebDriver``. String
   literals used by ``mock.patch`` are never treated as code.

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
FORBIDDEN_DRIVERS = {"chrome", "firefox", "edge", "safari", "webdriver"}
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
    """Top-level modules plus every local package (dir with __init__.py)."""
    modules = {path.stem for path in root.glob("*.py")}
    modules.update({path.parent.name for path in root.rglob("__init__.py")})
    return modules


def _import_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the top-level package name of every alias in an import."""
    if isinstance(node, ast.Import):
        return [
            alias.name.split(".")[0]
            for alias in node.names
            if alias.name and alias.name != "__future__"
        ]
    if isinstance(node, ast.ImportFrom):
        if node.module:
            return [node.module.split(".")[0]]
    return []


def _driver_call_receiver_is_webdriver(node: ast.AST, webdriver_names: set[str]) -> bool:
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return isinstance(current, ast.Name) and current.id in webdriver_names


def _collect_selenium_imports(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Collect names bound to the webdriver module and driver classes.

    Returns (webdriver_names, direct_classes). ``webdriver_names`` holds
    identifiers that resolve to the selenium webdriver module (``webdriver``,
    ``selenium``, or an alias such as ``wd``); ``direct_classes`` holds class
    names imported straight from selenium packages (``Chrome``, ``WebDriver``).
    """
    webdriver_names: set[str] = set()
    direct_classes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "selenium" or alias.name.startswith("selenium.webdriver"):
                    webdriver_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").casefold()
            if module != "selenium" and not module.startswith("selenium.webdriver"):
                continue
            for alias in node.names:
                if alias.name == "webdriver":
                    webdriver_names.add(alias.asname or alias.name)
                elif alias.name.casefold() in FORBIDDEN_DRIVERS:
                    direct_classes.add(alias.asname or alias.name)
    return webdriver_names, direct_classes


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
            for root in _import_roots(node):
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
        webdriver_names, direct_classes = _collect_selenium_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr.casefold() not in FORBIDDEN_DRIVERS:
                    continue
                if _driver_call_receiver_is_webdriver(func, webdriver_names):
                    findings.append(
                        f"{path}:{node.lineno}: direct {func.attr}() "
                        "construction via a selenium webdriver receiver is "
                        "forbidden in unit tests"
                    )
            elif isinstance(func, ast.Name) and func.id in direct_classes:
                findings.append(
                    f"{path}:{node.lineno}: direct {func.id}() construction "
                    "from a selenium import is forbidden in unit tests"
                )
    return findings


def audit(root: pathlib.Path) -> list[str]:
    """Run the full integrity audit against ``root`` and return findings."""
    files = _python_files(root)
    findings: list[str] = []
    syntax_findings, trees = _check_syntax(files)
    findings += syntax_findings
    findings += _check_duplicate_definitions(trees)
    findings += _check_imports(
        trees,
        stdlib=set(sys.stdlib_module_names),
        declared=_declared_dependencies(root),
        local=_local_modules(root),
    )
    findings += _check_test_driver_construction(trees)
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = pathlib.Path(args[0]).resolve() if args else ROOT
    files = _python_files(root)
    if not files:
        print("No Python files found to audit.")
        return 1

    findings = audit(root)
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
