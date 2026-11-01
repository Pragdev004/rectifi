"""The first lock on chart code the model wrote: a static check with the standard `ast` module. Pure, stdlib only.

It answers "may this code run at all?" and, when not, says why in plain words (the words go back to the model as repair
feedback). It is a filter for mistakes and prompt injection, NOT a security boundary: the sandbox (sandbox.py) is the second
lock and the figure checks (checks.py) are the third. Never weaken one because another exists.

The money rule is the heart of it (CLAUDE.md rule 1): code may not contain a typed rupee amount. Numbers above 100 must come
from the data (`df`) and be written with `inr()`.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ..money import amounts_in_text

MAX_BYTES = 8 * 1024
MAX_LINES = 200
MAX_NODES = 3000

ALLOWED_MODULES = frozenset({
    "pandas", "numpy", "matplotlib", "matplotlib.pyplot", "matplotlib.ticker", "matplotlib.dates",
    "seaborn", "math", "datetime", "calendar", "collections", "itertools", "textwrap",
})
PLOTLY_MODULES = frozenset({"plotly", "plotly.express", "plotly.graph_objects"})

# Names the code may not even mention. `type` is only refused with 3 arguments (it builds classes then).
FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr",
    "breakpoint", "exit", "quit", "help", "memoryview",
})

# Attributes that read or write files, run code, change the backend or reach the system.
FORBIDDEN_ATTRS = frozenset({
    "to_csv", "to_excel", "to_pickle", "to_parquet", "to_sql", "to_json", "to_html", "to_clipboard", "to_feather", "to_hdf",
    "to_stata", "to_xml", "to_orc", "to_gbq", "to_latex",
    "query", "eval", "savefig", "show", "write_image", "write_html", "write_json", "tofile", "fromfile", "load", "save",
    "memmap", "ctypeslib", "system", "popen", "environ", "use",
    "loadtxt", "genfromtxt", "savetxt", "savez", "savez_compressed", "fromregex", "DataSource", "imread", "imsave",
    "switch_backend", "ion", "ioff", "ExcelFile", "ExcelWriter", "HDFStore", "io", "compat", "testing",
    # modules the plotting libraries imported and left as attributes (matplotlib.os, calendar.sys.modules[...]) and what they offer
    "os", "sys", "subprocess", "importlib", "modules", "builtins", "shutil", "pathlib", "tempfile", "inspect", "socket", "ctypes",
    "signal", "kill", "killpg", "getppid", "listdir", "scandir", "walk", "FileIO", "getenv", "mro",
})
# read_csv, read_excel...; load_dataset downloads from the internet; f_back, gi_frame, cr_frame, ag_frame, tb_frame walk the call stack
FORBIDDEN_ATTR_PREFIXES = ("read_", "load", "f_", "gi_", "cr_", "ag_", "tb_")

# Keywords whose number is about layout, so it may be up to 1000.
LAYOUT_KEYWORDS = frozenset({
    "figsize", "dpi", "rotation", "fontsize", "size", "s", "linewidth", "lw", "markersize", "ms", "bins", "pad", "labelpad",
    "zorder", "alpha", "width", "height", "top", "n",
})

_MONEY_STRING = re.compile(r"(?i)(₹|\brs\.?|\binr)\s*\d")
_GROUPED_NUMBER = re.compile(r"\d{1,3}(,\d{2,3})+")
_FOUR_DIGITS = re.compile(r"\d{4,}")
_ISO_WHOLE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_YEAR_WHOLE = re.compile(r"^(19|20)\d{2}$")
_HEX_WHOLE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_ISO_INSIDE = re.compile(r"(?<!\d)\d{4}-\d{2}(-\d{2})?(?!\d)")
_YEAR_INSIDE = re.compile(r"(?<![\d,.])(199\d|20\d\d|2100)(?![\d,])")

NUMBER_MESSAGE = ("Numbers over 100 can't be typed into chart code (they could be made-up rupee amounts). "
                  "Compute them from df and format them with inr().")
STRING_MESSAGE = ("A rupee amount or a long number is typed inside a text. Never type amounts: compute the figure from df "
                  "and build the text with inr(...).")


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.message}"


@dataclass
class Validation:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def rules(self) -> list[str]:
        return [v.rule for v in self.violations]

    def feedback(self, limit: int = 6) -> str:
        """The violations as plain lines for the repair prompt."""
        lines = [str(v) for v in self.violations[:limit]]
        if len(self.violations) > limit:
            lines.append(f"...and {len(self.violations) - limit} more.")
        return "\n".join(lines)


def plotly_enabled() -> bool:
    return os.getenv("RECTIFI_CHARTS_PLOTLY", "").strip().lower() in ("1", "true", "yes", "on")


def allowed_modules(allow_plotly: bool | None = None) -> frozenset[str]:
    on = plotly_enabled() if allow_plotly is None else allow_plotly
    return ALLOWED_MODULES | PLOTLY_MODULES if on else ALLOWED_MODULES


def extract_code(text: str) -> str:
    """The Python inside a model reply: the first ```python fence, else any fence, else the whole reply."""
    text = text or ""
    for pattern in (r"```(?:python|py)[ \t]*\r?\n(.*?)```", r"```[a-zA-Z]*[ \t]*\r?\n(.*?)```"):
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip("\n") + "\n"
    open_fence = re.search(r"```(?:python|py)?[ \t]*\r?\n(.*)", text, re.DOTALL | re.IGNORECASE)   # reply cut off before the closing fence
    if open_fence:
        return open_fence.group(1).strip("\n") + "\n"
    return text.strip("\n") + "\n"


def _fold(node: ast.AST):
    """The value of a tree made only of number constants and + - * / // % **, else None. Stops huge powers."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _fold(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 12 or abs(left) > 1e6:
                    return float("inf")
                return left ** right
        except (ArithmeticError, ValueError, OverflowError):
            return None
    return None


def _string_problem(value: str) -> bool:
    if _ISO_WHOLE.match(value) or _YEAR_WHOLE.match(value) or _HEX_WHOLE.match(value):
        return False
    if _MONEY_STRING.search(value) or _GROUPED_NUMBER.search(value):
        return True
    stripped = _YEAR_INSIDE.sub(" ", _ISO_INSIDE.sub(" ", value))        # a year or a date may sit inside a longer text
    return bool(_FOUR_DIGITS.search(stripped))


class _Checker:
    def __init__(self, request: str, modules: frozenset[str]):
        self.request = request
        self.modules = modules
        self.violations: list[Violation] = []
        try:
            self.typed = {Decimal(str(v)).quantize(Decimal("0.01")) for v in amounts_in_text(request)}
        except (InvalidOperation, ValueError):
            self.typed = set()
        self._seen: set[tuple[str, int | None]] = set()

    def add(self, rule: str, message: str, node: ast.AST | None = None) -> None:
        line = getattr(node, "lineno", None)
        key = (rule + message, line)
        if key in self._seen:
            return
        self._seen.add(key)
        self.violations.append(Violation(rule, message, line))

    # ── numbers ──
    def number_ok(self, value, in_layout: bool) -> bool:
        if isinstance(value, complex):
            return False
        try:
            magnitude = abs(value)
        except TypeError:
            return False
        if magnitude != magnitude or magnitude == float("inf"):
            return False
        if magnitude <= 100:
            return True
        if in_layout and magnitude <= 1000:
            return True
        if isinstance(value, int) and 1990 <= value <= 2100:
            return True
        try:
            return Decimal(str(abs(value))).quantize(Decimal("0.01")) in self.typed or Decimal(str(value)).quantize(Decimal("0.01")) in self.typed
        except (InvalidOperation, ValueError):
            return False

    def visit(self, node: ast.AST, layout: bool = False) -> None:
        """Walk with one piece of context: are we directly inside a layout keyword's value (tuples and signs only)?"""
        if isinstance(node, (ast.BinOp, ast.UnaryOp)):
            folded = _fold(node)
            if folded is not None:
                if not self.number_ok(folded, layout):
                    self.add("number", NUMBER_MESSAGE, node)
                return                                        # the pieces were folded: don't judge them again
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or value is None or value is Ellipsis:
                pass
            elif isinstance(value, (int, float, complex)):
                if not self.number_ok(value, layout):
                    self.add("number", NUMBER_MESSAGE, node)
            elif isinstance(value, str):
                if _string_problem(value):
                    self.add("money_string", STRING_MESSAGE, node)
                if "__" in value:
                    self.add("dunder_string", "A text contains a double underscore, which can be used to reach hidden attributes.", node)
            elif isinstance(value, bytes):
                self.add("bytes", "Byte strings are not allowed in chart code.", node)
        elif isinstance(node, ast.keyword):
            in_layout = node.arg in LAYOUT_KEYWORDS
            self.visit(node.value, in_layout)
            return
        elif isinstance(node, (ast.Tuple, ast.List, ast.UnaryOp)) and layout:
            for child in ast.iter_child_nodes(node):
                self.visit(child, True)
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child, False)


def _assigns_fig(body: list[ast.stmt]) -> bool:
    """Does module-level code (not inside a def or lambda) assign the name `fig`?"""

    def target_has_fig(target: ast.AST) -> bool:
        if isinstance(target, ast.Name):
            return target.id == "fig"
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(target_has_fig(t) for t in target.elts)
        if isinstance(target, ast.Starred):
            return target_has_fig(target.value)
        return False

    def walk(stmts: list[ast.stmt]) -> bool:
        for s in stmts:
            if isinstance(s, ast.Assign) and any(target_has_fig(t) for t in s.targets):
                return True
            if isinstance(s, (ast.AnnAssign, ast.AugAssign)) and target_has_fig(s.target) and getattr(s, "value", None) is not None:
                return True
            if isinstance(s, ast.With) and any(i.optional_vars is not None and target_has_fig(i.optional_vars) for i in s.items):
                return True
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for name in ("body", "orelse", "finalbody"):
                if walk(getattr(s, name, []) or []):
                    return True
            for handler in getattr(s, "handlers", []) or []:
                if walk(handler.body):
                    return True
            for case in getattr(s, "cases", []) or []:
                if walk(case.body):
                    return True
        return False

    return walk(body)


def validate(code: str, *, request: str = "", allow_plotly: bool | None = None) -> Validation:
    """Check chart code. `request` is what the person typed: a number they typed may appear in the code."""
    if not isinstance(code, str) or not code.strip():
        return Validation(False, [Violation("empty", "The reply had no code. Write Python that ends with `fig` set to the figure.")])
    size = len(code.encode("utf-8", "replace"))
    if size > MAX_BYTES:
        return Validation(False, [Violation("size", f"The code is {size} bytes; the limit is {MAX_BYTES}. Make it shorter.")])
    if code.count("\n") + 1 > MAX_LINES:
        return Validation(False, [Violation("size", f"The code is longer than {MAX_LINES} lines. Make it shorter.")])
    if "\x00" in code:
        return Validation(False, [Violation("syntax", "The code contains a null character.")])
    try:
        tree = ast.parse(code, filename="<chart>", mode="exec")
    except SyntaxError as exc:
        return Validation(False, [Violation("syntax", f"Syntax error: {exc.msg}", exc.lineno)])
    except (ValueError, RecursionError, MemoryError):
        return Validation(False, [Violation("syntax", "The code can't be parsed (it is too deeply nested).")])

    count = 0
    for _ in ast.walk(tree):
        count += 1
        if count > MAX_NODES:
            return Validation(False, [Violation("size", f"The code has more than {MAX_NODES} syntax nodes. Make it simpler.")])

    modules = allowed_modules(allow_plotly)
    checker = _Checker(request, modules)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in modules:
                    checker.add("import", f"Importing '{alias.name}' is not allowed. Allowed: {', '.join(sorted(modules))}.", node)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                checker.add("import", "Relative imports are not allowed.", node)
            elif node.module not in modules:
                checker.add("import", f"Importing from '{node.module}' is not allowed. Allowed: {', '.join(sorted(modules))}.", node)
            else:
                for alias in node.names:
                    if alias.name == "*":
                        checker.add("import", "'from ... import *' is not allowed.", node)
                    elif alias.name.startswith("_") or alias.name in FORBIDDEN_ATTRS or alias.name.startswith(FORBIDDEN_ATTR_PREFIXES):
                        checker.add("import", f"Importing the name '{alias.name}' is not allowed.", node)
                    elif f"{node.module}.{alias.name}" not in modules and alias.name[:1].islower() and node.module in ("matplotlib", "pandas", "numpy", "seaborn") \
                            and alias.name in ("cbook", "backends", "testing", "io", "compat", "util", "lib", "core", "f2py", "distutils", "ctypeslib", "rcsetup", "_api"):
                        checker.add("import", f"Importing the submodule '{alias.name}' is not allowed.", node)
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            checker.add("syntax_kind", "Classes and async code are not allowed in chart code. Use plain statements and functions.", node)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES or (node.id.startswith("__") and node.id != "__"):
                checker.add("forbidden_name", f"The name '{node.id}' is not allowed.", node)
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_"):
                checker.add("forbidden_attr", f"Attributes that start with an underscore ('{attr}') are not allowed.", node)
            elif attr in FORBIDDEN_ATTRS or attr in FORBIDDEN_NAMES or attr.startswith(FORBIDDEN_ATTR_PREFIXES):
                checker.add("forbidden_attr", f"The attribute '.{attr}' is not allowed (it reads or writes files, runs code or changes the system). "
                                              "The harness saves the figure for you; just set `fig`.", node)
        elif isinstance(node, (ast.FunctionDef, ast.arg)):
            name = node.name if isinstance(node, ast.FunctionDef) else node.arg
            if name.startswith("__"):
                checker.add("forbidden_name", f"The name '{name}' is not allowed.", node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type" and len(node.args) == 3:
            checker.add("forbidden_name", "type() with three arguments builds classes and is not allowed.", node)

    try:
        checker.visit(tree)
    except RecursionError:
        checker.add("syntax", "The code is too deeply nested.")

    if not _assigns_fig(tree.body):
        checker.add("contract", "The code must assign the figure to a variable named `fig` at the top level "
                                "(for example `fig, ax = plt.subplots(figsize=(10, 6))`).")
    return Validation(not checker.violations, checker.violations)
