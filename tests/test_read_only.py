"""Render API keys can deploy, suspend and delete. This package must only ever read.

These tests inspect the integration's source, so a write call added anywhere later fails CI.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).parent.parent / "custom_components" / "render_monitor"
WRITE_METHODS = {"post", "put", "patch", "delete", "request", "head", "options", "ws_connect"}
WRITE_VERBS = re.compile(r"^(POST|PUT|PATCH|DELETE)$", re.IGNORECASE)
HTTP_LIBRARIES = {"requests", "httpx", "urllib3", "http.client", "urllib.request"}


def python_files() -> list[Path]:
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, "integration sources not found"
    return files


def test_no_write_http_methods_anywhere():
    problems = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in WRITE_METHODS:
                    problems.append(f"{path.name}:{node.lineno} calls .{node.func.attr}()")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and WRITE_VERBS.match(node.value):
                problems.append(f"{path.name}:{node.lineno} mentions {node.value!r}")
            if isinstance(node, ast.keyword) and node.arg == "method":
                problems.append(f"{path.name}:{node.lineno} passes method=")
    assert not problems, "write access found:\n" + "\n".join(problems)


def test_no_other_http_client_libraries():
    problems = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            problems += [f"{path.name}:{node.lineno} imports {n}" for n in names if n in HTTP_LIBRARIES]
    assert not problems, "\n".join(problems)


def test_only_the_client_touches_the_session_and_only_with_get():
    """The aiohttp session is used in exactly one place: RenderClient._get, via session.get."""
    uses = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "_session"):
                uses.append((path.name, node.attr))
    assert uses == [("api.py", "get")]


def test_render_api_url_only_in_the_client():
    """No other module (or the card) talks to api.render.com directly."""
    offenders = [
        p.name for p in PACKAGE.rglob("*")
        if p.is_file() and p.suffix in {".py", ".js"} and p.name != "api.py"
        and "api.render.com" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
