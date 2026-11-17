"""Static checks on generated strategy code, run before it is ever imported.

This stops a confused model from writing a strategy that touches files, the
network, or the live-trading parts of Nautilus. It is a seatbelt, not a
sandbox: the real boundary is the dedicated OS user lox runs as.
"""

from __future__ import annotations

import ast
from pathlib import Path

from lox.api import RESERVED

MAX_BYTES = 60_000

# Import roots a strategy may use. Anything else is rejected.
ALLOWED_IMPORTS = (
    "__future__",
    "bisect",
    "collections",
    "dataclasses",
    "datetime",
    "decimal",
    "enum",
    "functools",
    "heapq",
    "itertools",
    "lox.api",
    "math",
    "nautilus_trader.indicators",
    "nautilus_trader.model",
    "numpy",
    "pandas",
    "statistics",
    "typing",
    "zoneinfo",
)

BANNED_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "delattr",
        "exit",
        "quit",
        "memoryview",
    }
)

# Attribute names that mean file, process or network access on numpy/pandas
# objects (or anything else).
BANNED_ATTRS = frozenset(
    {
        "read_csv",
        "read_parquet",
        "read_pickle",
        "read_json",
        "read_html",
        "read_sql",
        "read_excel",
        "read_feather",
        "read_hdf",
        "read_table",
        "read_clipboard",
        "to_csv",
        "to_parquet",
        "to_pickle",
        "to_json",
        "to_sql",
        "to_excel",
        "to_feather",
        "to_hdf",
        "to_clipboard",
        "load",
        "save",
        "savez",
        "savetxt",
        "loadtxt",
        "genfromtxt",
        "fromfile",
        "tofile",
        "memmap",
        "system",
        "popen",
        "ctypeslib",
        "f2py",
        "distutils",
        "testing",
    }
)

TOP_LEVEL = (
    ast.Import,
    ast.ImportFrom,
    ast.ClassDef,
    ast.FunctionDef,
    ast.Assign,
    ast.AnnAssign,
)


def _allowed(module: str) -> bool:
    return any(
        module == root or module.startswith(root + ".") for root in ALLOWED_IMPORTS
    )


def check_source(source: str) -> list[str]:
    """Return a list of problems; empty means the code may be loaded."""
    if len(source.encode()) > MAX_BYTES:
        return [f"file is larger than {MAX_BYTES} bytes"]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"line {exc.lineno}: syntax error: {exc.msg}"]

    problems: list[str] = []

    def bad(node: ast.AST, message: str) -> None:
        problems.append(f"line {getattr(node, 'lineno', '?')}: {message}")

    for node in tree.body:
        is_docstring = isinstance(node, ast.Expr) and isinstance(
            node.value, ast.Constant
        )
        if not isinstance(node, TOP_LEVEL) and not is_docstring:
            bad(
                node,
                "only imports, classes, functions and constants are allowed at module level",
            )

    strategy_classes = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _allowed(alias.name):
                    bad(node, f"import of '{alias.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or not _allowed(module):
                bad(node, f"import from '{'.' * node.level}{module}' is not allowed")
            if any(alias.name == "*" for alias in node.names):
                bad(node, "star imports are not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            bad(node, f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                bad(node, f"dunder attribute '{node.attr}' is not allowed")
            elif node.attr in BANNED_ATTRS:
                bad(
                    node,
                    f"'.{node.attr}' is not allowed (no file, process or network access)",
                )
            elif node.attr.startswith("_lox"):
                bad(node, f"'{node.attr}' is harness state; do not touch it")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"getattr", "setattr", "hasattr"}:
                name = node.args[1] if len(node.args) > 1 else None
                literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
                if not literal or name.value.startswith("_"):
                    bad(
                        node,
                        f"{node.func.id}() needs a literal, non-underscore attribute name",
                    )
        elif isinstance(node, ast.ExceptHandler):
            caught = node.type
            names = {getattr(caught, "id", None)} | {
                getattr(e, "id", None) for e in getattr(caught, "elts", [])
            }
            if caught is None or names & {
                "BaseException",
                "Deadline",
                "KeyboardInterrupt",
            }:
                bad(
                    node,
                    "catch specific exceptions; bare except and BaseException are not allowed",
                )
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bad(node, "global and nonlocal are not allowed")
        elif isinstance(node, ast.ClassDef):
            bases = {
                b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                for b in node.bases
            }
            if "LoxStrategy" in bases:
                strategy_classes += 1
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name in {"__init__", "__new__", "__init_subclass__"}:
                    bad(item, f"do not define {item.name}; build state in setup()")
                elif item.name in RESERVED or item.name.startswith("_lox"):
                    bad(
                        item,
                        f"'{item.name}' is provided by LoxStrategy; do not redefine it",
                    )

    if not strategy_classes:
        problems.append("no class subclasses LoxStrategy directly")
    return problems


def check_file(path: Path) -> list[str]:
    if not path.exists():
        return [f"{path} does not exist"]
    return check_source(path.read_text())
