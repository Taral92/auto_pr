"""What this change touches, computed once before the first model turn.

The live trace that started all of this spent ten iterations guessing: four
speculative regexes returning nothing, and not one read of a changed file. The
agent was not being stupid - it had a diff and two search tools, and no way to
learn what calls what except by asking. So it asked, badly, and the budget went.

Everything it was groping for is derivable in Python, deterministically, before
the run starts: which symbols the diff touched, who else defines or calls them,
who imports the changed modules, which tests name them. This module computes
that map; `assemble_context` puts it in the first message.

Two boundaries it must not cross, and does not:

- The map REPORTS paths outside the diff. It does not make them readable. The
  scope jail is untouched, and the rendered map says so in as many words, so a
  path here is a lead to be reasoned about, not a file to try to open.
- The map goes in the MESSAGE, never in the corpus. Grounding matches evidence
  against the corpus, so nothing here can be quoted back as proof.

AST, not grep. `store/base.py:23` mentions `put_many` in a docstring; a regex
reports it as a call site and the agent chases prose.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

#: Directories never walked. A vendored tree would swamp the map and the budget.
IGNORE_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
        "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", "dist", "build", ".eggs", "site-packages",
    }
)

#: Bounds. The map is cached, so its cost is paid once - but it still occupies
#: the window, and a 60-file PR must not push the diff out of the way.
MAX_FILES_SCANNED = 2000
MAX_SYMBOLS = 40
MAX_REFS_PER_SYMBOL = 8
MAX_IMPORTERS = 15
MAX_TESTS = 15
MAX_RENDER_CHARS = 8000

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    kind: str            # def | use | import
    text: str            # the source line, stripped

    def render(self) -> str:
        return f"{self.path}:{self.line}  {self.text}"


@dataclass(frozen=True)
class ChangedSymbol:
    path: str
    name: str            # qualified: Store.put_many
    kind: str            # function | class | method
    line: int
    end_line: int
    references: tuple[Reference, ...] = ()


@dataclass(frozen=True)
class ChangeMap:
    symbols: tuple[ChangedSymbol, ...] = ()
    importers: tuple[Reference, ...] = ()
    tests: tuple[Reference, ...] = ()
    unparsed: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.symbols or self.importers or self.tests or self.unparsed)

    def render(self) -> str:
        """The block the model reads. Empty string when there is nothing to say."""
        if self.empty:
            return ""
        out: list[str] = [
            "CHANGE MAP (computed from the diff, not from the model):",
        ]
        if self.symbols:
            out.append("")
            out.append("Symbols this diff changed, and where else they appear:")
            for symbol in self.symbols:
                out.append(f"  {symbol.path}:{symbol.line}  {symbol.name} ({symbol.kind})")
                for ref in symbol.references:
                    out.append(f"      {ref.kind}  {ref.render()}")
                if not symbol.references:
                    out.append("      (no references outside this diff)")
        if self.importers:
            out.append("")
            out.append("Files outside this diff that import a changed module:")
            out.extend(f"  {ref.render()}" for ref in self.importers)
        if self.tests:
            out.append("")
            out.append("Tests outside this diff that name something this diff changed:")
            out.extend(f"  {ref.render()}" for ref in self.tests)
        if self.unparsed:
            out.append("")
            out.append(
                "Changed files that could not be parsed for symbols: "
                + ", ".join(self.unparsed)
            )
        out.append("")
        out.append(
            "Every path above that is not in CHANGED FILES is outside this "
            "review: read_file still refuses it, and nothing quoted from this "
            "map counts as evidence. Use it to decide what to read and what to "
            "search for in the files you CAN read."
        )
        text = "\n".join(out)
        if len(text) > MAX_RENDER_CHARS:
            text = text[:MAX_RENDER_CHARS] + "\n  [change map truncated]"
        return text


# -- building ---------------------------------------------------------------


def build(
    repo_root: str | None,
    scope: dict[str, str],
    hunks: dict[str, list[tuple[int, int]]],
) -> ChangeMap:
    """The map for one diff. Deterministic: every list is sorted.

    Degrades to an empty map rather than raising - a missing checkout, an
    unparseable file or a non-Python repository costs the agent a hint, and
    must never cost it the review.
    """
    if not repo_root:
        return ChangeMap()
    root = Path(repo_root)
    if not root.is_dir():
        return ChangeMap()

    changed = {path for path, status in scope.items() if status != "deleted"}
    symbols, unparsed = _changed_symbols(root, changed, hunks)
    names = {symbol.name.rsplit(".", 1)[-1] for symbol in symbols}
    modules = {_module_name(path) for path in changed}
    modules.discard("")

    files = _python_files(root)
    outside = [path for path in files if path not in changed]
    refs, imports = _scan(root, outside, names, modules)

    # Only trust a reference from a file that imports something this diff
    # changed. A bare name match is far too loose for short method names:
    # `Store.get` otherwise "matches" `@router.get("/healthz")` in api/health.py
    # and `_span.get()` in telemetry/trace.py, and a map that points at those
    # sends the agent chasing unrelated code - the exact failure it exists to
    # stop. A caller reaches a method through its class, and to have the class
    # it has to import it.
    importing = {ref.path for ref in imports}
    refs = {
        name: [ref for ref in found if ref.path in importing]
        for name, found in refs.items()
    }

    symbols = tuple(
        ChangedSymbol(
            path=symbol.path,
            name=symbol.name,
            kind=symbol.kind,
            line=symbol.line,
            end_line=symbol.end_line,
            references=tuple(
                sorted(
                    refs.get(symbol.name.rsplit(".", 1)[-1], ()),
                    key=lambda r: (r.path, r.line),
                )[:MAX_REFS_PER_SYMBOL]
            ),
        )
        for symbol in symbols
    )
    importers = sorted(imports, key=lambda r: (r.path, r.line))
    candidates = [ref for ref in importers if _is_test(ref.path)]
    candidates += [
        ref
        for refs_for_name in refs.values()
        for ref in refs_for_name
        if _is_test(ref.path)
    ]
    # One line per test FILE. A test that both imports a changed module and
    # names a changed symbol is one lead, not two.
    by_path: dict[str, Reference] = {}
    for ref in sorted(set(candidates), key=lambda r: (r.path, r.line)):
        by_path.setdefault(ref.path, ref)
    tests = list(by_path.values())
    return ChangeMap(
        symbols=symbols[:MAX_SYMBOLS],
        importers=tuple(importers[:MAX_IMPORTERS]),
        tests=tuple(tests[:MAX_TESTS]),
        unparsed=unparsed,
    )


def _python_files(root: Path) -> list[str]:
    """Every .py file under the checkout, sorted, bounded, jail-safe."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if len(found) >= MAX_FILES_SCANNED:
            break
        if path.is_symlink() or not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue          # a symlink escaping the checkout
        if IGNORE_DIRS & set(rel.parts):
            continue
        found.append(str(rel))
    return found


def _module_name(rel: str) -> str:
    if not rel.endswith(".py"):
        return ""
    parts = rel[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_test(rel: str) -> bool:
    name = Path(rel).name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in Path(rel).parts
    )


def _parse(root: Path, rel: str) -> tuple[ast.AST | None, list[str]]:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, []
    try:
        return ast.parse(text), text.splitlines()
    except SyntaxError:
        return None, text.splitlines()


def _changed_symbols(
    root: Path, changed: set[str], hunks: dict[str, list[tuple[int, int]]]
) -> tuple[list[ChangedSymbol], tuple[str, ...]]:
    symbols: list[ChangedSymbol] = []
    unparsed: list[str] = []
    for rel in sorted(changed):
        ranges = hunks.get(rel) or []
        if not ranges:
            continue
        tree, _ = _parse(root, rel)
        if tree is None:
            # Only a .py file that failed to parse is worth reporting. A .ts
            # or .go file is not a parse failure, it is outside what this
            # analyser covers, and naming every one of them would turn the map
            # for a front-end PR into a wall of noise.
            if rel.endswith(".py"):
                unparsed.append(rel)
            continue
        symbols.extend(_intersecting(tree, rel, ranges))
    return sorted(symbols, key=lambda s: (s.path, s.line)), tuple(unparsed)


def _intersecting(
    tree: ast.AST, rel: str, ranges: list[tuple[int, int]], prefix: str = ""
) -> list[ChangedSymbol]:
    """Innermost definitions whose body overlaps a hunk.

    Innermost matters: a changed method overlaps its class too, and reporting
    both says the whole class changed when one method did.
    """
    out: list[ChangedSymbol] = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, _DEF_NODES):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or node.lineno
        if not any(start <= hi and lo <= end for lo, hi in ranges):
            continue
        name = f"{prefix}{node.name}"
        inner = _intersecting(node, rel, ranges, prefix=f"{name}.")
        if inner:
            out.extend(inner)
            continue
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else "method" if prefix else "function"
        )
        out.append(
            ChangedSymbol(path=rel, name=name, kind=kind, line=start, end_line=end)
        )
    return out


def _scan(
    root: Path, files: list[str], names: set[str], modules: set[str]
) -> tuple[dict[str, list[Reference]], list[Reference]]:
    """One AST pass per out-of-scope file, collecting both answers at once."""
    refs: dict[str, list[Reference]] = {}
    imports: list[Reference] = []
    for rel in files:
        tree, lines = _parse(root, rel)
        if tree is None:
            continue

        def line_text(lineno: int) -> str:
            return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""

        for node in ast.walk(tree):
            if isinstance(node, _DEF_NODES) and node.name in names:
                refs.setdefault(node.name, []).append(
                    Reference(rel, node.lineno, "def", line_text(node.lineno))
                )
            elif isinstance(node, ast.Name) and node.id in names:
                refs.setdefault(node.id, []).append(
                    Reference(rel, node.lineno, "use", line_text(node.lineno))
                )
            elif isinstance(node, ast.Attribute) and node.attr in names:
                refs.setdefault(node.attr, []).append(
                    Reference(rel, node.lineno, "use", line_text(node.lineno))
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if _imports_changed(node, rel, modules):
                    imports.append(
                        Reference(rel, node.lineno, "import", line_text(node.lineno))
                    )
    for name in refs:
        refs[name] = sorted(set(refs[name]), key=lambda r: (r.path, r.line))
    return refs, imports


def _imports_changed(node: ast.AST, rel: str, modules: set[str]) -> bool:
    if isinstance(node, ast.Import):
        return any(_touches(alias.name, modules) for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return False
    base = node.module or ""
    if node.level:
        # Relative: resolve against this file's package.
        package = _module_name(rel).split(".")[:-1]
        if node.level > 1:
            package = package[: -(node.level - 1)] or []
        base = ".".join([*package, base]) if base else ".".join(package)
    if _touches(base, modules):
        return True
    return any(_touches(f"{base}.{alias.name}" if base else alias.name, modules)
               for alias in node.names)


def _touches(module: str, modules: set[str]) -> bool:
    """`store.base` is touched by importing `store.base`, not by `store.basex`."""
    return bool(module) and any(
        module == changed or module.startswith(changed + ".") for changed in modules
    )
