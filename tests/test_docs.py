from __future__ import annotations

import ast
import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import django
import pytest

API_RST = Path(__file__).parent.parent / "docs" / "api.rst"

DIRECTIVE_RE = re.compile(
    r"^(?P<indent> *)\.\. (?P<kind>class|exception|function|method|classmethod"
    r"|property|attribute):: (?P<name>[\w.]+)(?:\((?P<params>.*)\))?$"
)
CURRENTMODULE_RE = re.compile(r"^\.\. currentmodule:: (?P<module>[\w.]+)$")


def documented_objects() -> list[tuple[str, str, str, str | None]]:
    """
    Yield (kind, dotted path, name, params) for each object documented in
    api.rst, with indented directives qualified by the enclosing class.
    """
    module = ""
    enclosing = ""
    found = []
    for line in API_RST.read_text().splitlines():
        if match := CURRENTMODULE_RE.match(line):
            module = match["module"]
            continue
        match = DIRECTIVE_RE.match(line)
        if match is None:
            continue
        kind, name, params = match["kind"], match["name"], match["params"]
        if match["indent"]:
            path = f"{enclosing}.{name}"
        else:
            path = f"{module}.{name}"
            if kind in ("class", "exception"):
                enclosing = path
        found.append((kind, path, name, params))
    return found


def resolve(path: str) -> Any:
    """Import the dotted path, trying progressively shorter module prefixes."""
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:i])
        try:
            obj = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        if module_name.endswith(".tasks") and django.VERSION < (6, 0):
            pytest.skip("Django tasks framework, 6.0+")
        for attr in parts[i:]:
            obj = getattr(obj, attr)
        return obj
    raise AssertionError(f"Cannot import {path}")  # pragma: no cover


def parse_documented_params(params: str) -> list[tuple[str, str, str | None]]:
    """Parse a documented parameter list into (name, kind, default repr)."""
    args = ast.parse(f"def f({params}): pass").body[0].args  # type: ignore[attr-defined]
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(args.defaults)
    ) + list(args.defaults)
    result = [
        (arg.arg, "positional", None if d is None else ast.unparse(d))
        for arg, d in zip(positional, defaults)
    ]
    result.extend(
        (arg.arg, "keyword", None if d is None else ast.unparse(d))
        for arg, d in zip(args.kwonlyargs, args.kw_defaults)
    )
    return result


def actual_params(obj: Any, *, drop_self: bool) -> list[tuple[str, str, str | None]]:
    params = list(inspect.signature(obj).parameters.values())
    if drop_self:
        params = params[1:]
    return [
        (
            p.name,
            "keyword" if p.kind is p.KEYWORD_ONLY else "positional",
            None if p.default is p.empty else repr(p.default),
        )
        for p in params
    ]


DOCUMENTED = documented_objects()


@pytest.mark.parametrize(
    "kind,path,name,params", DOCUMENTED, ids=[path for _, path, _, _ in DOCUMENTED]
)
def test_documented_object(kind: str, path: str, name: str, params: str | None) -> None:
    obj = resolve(path)
    # A task from Django's tasks framework wraps the documented function.
    obj = getattr(obj, "func", obj)

    if params is None:
        # Documented without a signature, for example a model or attribute:
        # existing is enough.
        return

    if kind in ("class", "exception"):
        # inspect.signature() of a class already omits self.
        actual = actual_params(obj, drop_self=False)
    elif kind == "method":
        # Reached through the class, so the function still lists self.
        actual = actual_params(obj, drop_self=True)
    else:
        # Functions, and classmethods, which are bound to the class.
        actual = actual_params(obj, drop_self=False)

    assert parse_documented_params(params) == actual
