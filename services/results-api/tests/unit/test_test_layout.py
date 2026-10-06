"""Integration tests must not import from tests/unit/ (shared helpers live in tests/common.py)."""

import ast
from pathlib import Path

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"


def imported_modules(path: Path) -> list[tuple[int, str]]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
    return modules


def test_integration_tests_do_not_import_unit_test_modules():
    offending = [
        f"{path.name}:{line} imports {module}"
        for path in sorted(INTEGRATION_DIR.glob("*.py"))
        for line, module in imported_modules(path)
        if module == "tests.unit" or module.startswith("tests.unit.")
    ]

    assert offending == []
