"""Convention anti-pattern rules engine (Phase 1).

Executable rules that detect structural anti-patterns in changed files.
Called by convention_preflight_context when changed_files is provided.

Phase 1 rules:
- no_hardcoded_paths: Flags /Workspace/Users/, C:\\Users\\, C:/Users/
- no_deep_ternaries: Flags nested IfExp (ternary-in-ternary)

Phase 2 rules:
- boundary_message_match: Flags >= N with "Maximum: N" (off-by-one)
- no_unbound_variables: Flags vars assigned only in if-branch but used after

Phase 3 rules:
- no_dead_branches: Flags unreachable code after return/raise/continue/break
- list_or_single_documented: Flags str params iterated or passed to .extend()

Phase 4 rules (Windows compatibility):
- path_separator: Flags str(path.relative_to(...)) without .replace("\\", "/")
- write_text_encoding: Flags .write_text() without encoding="utf-8"
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ConventionViolation:
    """A single anti-pattern violation found in source code."""

    rule: str  # Rule name (e.g., "no_hardcoded_paths")
    file: str  # Relative file path
    line: int | None  # Line number (None for file-level)
    message: str  # Human-readable explanation
    severity: str  # "warning" or "blocker"
    fix_hint: str  # Actionable fix suggestion


# ---------------------------------------------------------------------------
# Path patterns (Rule 1)
# ---------------------------------------------------------------------------

_PATH_PATTERNS = [
    re.compile(r'/Workspace/Users/\S+'),
    re.compile(r'[A-Z]:\\Users\\\S+'),
    re.compile(r'[A-Z]:/Users/\S+'),
]


def _check_no_hardcoded_paths(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Scan changed files for hardcoded environment paths.

    Skips lines that are pure comments (leading #).
    """
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source_lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        in_docstring = False
        for line_no, line in enumerate(source_lines, start=1):
            stripped = line.lstrip()
            # Track triple-quoted docstrings
            triple_count = stripped.count('"""') + stripped.count("'''")
            if triple_count % 2 == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            # Skip comment-only lines
            if stripped.startswith("#"):
                continue
            for pattern in _PATH_PATTERNS:
                if pattern.search(line):
                    violations.append(ConventionViolation(
                        rule="no_hardcoded_paths",
                        file=rel_path,
                        line=line_no,
                        message=f"Hardcoded environment path detected: {line.strip()!r}",
                        severity="warning",
                        fix_hint="Use a configuration variable or relative path instead of absolute user paths.",
                    ))
                    break  # One violation per line is enough

    return violations


# ---------------------------------------------------------------------------
# Deep ternary detection (Rule 2)
# ---------------------------------------------------------------------------


class _TernaryVisitor(ast.NodeVisitor):
    """AST visitor that finds nested ternary expressions (IfExp in IfExp)."""

    def __init__(self, rel_path: str) -> None:
        self.violations: list[ConventionViolation] = []
        self._rel_path = rel_path

    def visit_IfExp(self, node: ast.IfExp) -> None:  # noqa: N802
        # Check if any child of this IfExp is also an IfExp
        for child in ast.walk(node):
            if child is node:
                continue
            if isinstance(child, ast.IfExp):
                self.violations.append(ConventionViolation(
                    rule="no_deep_ternaries",
                    file=self._rel_path,
                    line=node.lineno,
                    message="Nested ternary expression detected — hard to read and error-prone.",
                    severity="warning",
                    fix_hint="Refactor into an explicit if/elif/else block for clarity.",
                ))
                # Don't recurse further into this IfExp tree
                return
        # Only visit children if this node itself is clean
        self.generic_visit(node)


def _check_no_deep_ternaries(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Find nested ternary expressions (IfExp within IfExp) in changed files."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue

        visitor = _TernaryVisitor(rel_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations




# ---------------------------------------------------------------------------
# Boundary message match (Rule 3)
# ---------------------------------------------------------------------------

# Matches patterns like: "Maximum: 4", "Minimum: 10", "max: 5"
_BOUNDARY_MSG_PATTERN = re.compile(
    r'(Maximum|Minimum|max|min)[:\s]+(\d+)', re.IGNORECASE
)


class _BoundaryVisitor(ast.NodeVisitor):
    """Find comparisons near raise statements with boundary messages."""

    def __init__(self, rel_path: str, source_lines: list[str]) -> None:
        self.violations: list[ConventionViolation] = []
        self._rel_path = rel_path
        self._lines = source_lines

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        # Check if the body contains a Raise with a boundary message
        for stmt in node.body:
            if not isinstance(stmt, ast.Raise):
                continue
            # Extract the string from the raise
            msg = self._extract_raise_message(stmt)
            if not msg:
                continue
            match = _BOUNDARY_MSG_PATTERN.search(msg)
            if not match:
                continue

            keyword = match.group(1).lower()  # "maximum" or "minimum"
            stated_value = int(match.group(2))

            # Now check the if-condition for a comparison with the same number
            mismatch = self._check_comparison_mismatch(
                node.test, keyword, stated_value
            )
            if mismatch:
                self.violations.append(ConventionViolation(
                    rule="boundary_message_match",
                    file=self._rel_path,
                    line=node.lineno,
                    message=mismatch,
                    severity="warning",
                    fix_hint="Use '>' for maximum checks (>= N means effective limit is N-1). "
                             "Use '<' for minimum checks.",
                ))
        self.generic_visit(node)

    def _extract_raise_message(self, node: ast.Raise) -> str | None:
        """Extract string content from a Raise node's exception arguments."""
        exc = node.exc
        if exc is None:
            return None
        # raise Error("message") or raise Error(f"...")
        if isinstance(exc, ast.Call) and exc.args:
            arg = exc.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            if isinstance(arg, ast.JoinedStr):
                # f-string: collect Constant parts
                parts = []
                for val in arg.values:
                    if isinstance(val, ast.Constant):
                        parts.append(str(val.value))
                    elif isinstance(val, ast.FormattedValue):
                        # Try to get the variable name for simple cases
                        parts.append("{...}")
                return "".join(parts)
        return None

    def _check_comparison_mismatch(
        self, test: ast.expr, keyword: str, stated_value: int
    ) -> str | None:
        """Check if a comparison operator mismatches the stated boundary."""
        comparisons = self._extract_comparisons(test)
        for op, comparator_value in comparisons:
            if comparator_value != stated_value:
                continue
            # >= N with "Maximum: N" → effective limit is N-1 (mismatch)
            if keyword in ("maximum", "max") and isinstance(op, ast.GtE):
                return (
                    f"'>= {stated_value}' with 'Maximum: {stated_value}' — "
                    f"effective limit is {stated_value - 1}. Use '> {stated_value}' or fix message."
                )
            # <= N with "Minimum: N" → effective limit is N+1 (mismatch)
            if keyword in ("minimum", "min") and isinstance(op, ast.LtE):
                return (
                    f"'<= {stated_value}' with 'Minimum: {stated_value}' — "
                    f"effective limit is {stated_value + 1}. Use '< {stated_value}' or fix message."
                )
        return None

    def _extract_comparisons(
        self, node: ast.expr
    ) -> list[tuple[ast.cmpop, int | None]]:
        """Extract (operator, numeric_value) pairs from a Compare node."""
        results: list[tuple[ast.cmpop, int | None]] = []
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                val = self._get_int_value(comparator)
                if val is not None:
                    results.append((op, val))
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                results.extend(self._extract_comparisons(value))
        return results

    def _get_int_value(self, node: ast.expr) -> int | None:
        """Extract integer value from a Constant or Name node."""
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        return None


def _check_boundary_message_match(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Find boundary comparisons where operator doesn't match stated limit."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue

        source_lines = source.splitlines()
        visitor = _BoundaryVisitor(rel_path, source_lines)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations


# ---------------------------------------------------------------------------
# Unbound variables (Rule 4)
# ---------------------------------------------------------------------------


class _UnboundVariableVisitor(ast.NodeVisitor):
    """Find variables assigned only in if-branches but used after."""

    def __init__(self, rel_path: str) -> None:
        self.violations: list[ConventionViolation] = []
        self._rel_path = rel_path

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check_function_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._check_function_body(node)

    def _check_function_body(self, func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Check a function body for potentially unbound variables."""
        body = func_node.body

        for i, stmt in enumerate(body):
            if not isinstance(stmt, ast.If):
                continue

            # If the if-body always exits (return/raise/continue/break),
            # code after is only reachable when condition is False — safe.
            if self._branch_always_exits(stmt.body):
                continue

            # Find variables assigned ONLY in the if-body (not in else)
            if_assigns = self._get_assigned_names(stmt.body)
            else_assigns = self._get_assigned_names(stmt.orelse) if stmt.orelse else set()

            # Variables assigned in if but NOT in else
            potentially_unbound = if_assigns - else_assigns

            if not potentially_unbound:
                continue

            # Check variables assigned BEFORE this if statement
            prior_assigns: set[str] = set()
            for prior_stmt in body[:i]:
                prior_assigns.update(self._get_assigned_names([prior_stmt]))

            # Also include function parameters
            for arg in func_node.args.args + func_node.args.kwonlyargs:
                prior_assigns.add(arg.arg)
            if func_node.args.vararg:
                prior_assigns.add(func_node.args.vararg.arg)
            if func_node.args.kwarg:
                prior_assigns.add(func_node.args.kwarg.arg)

            # Remove variables that were initialized before
            truly_unbound = potentially_unbound - prior_assigns

            if not truly_unbound:
                continue

            # Check if any of these are used AFTER the if statement
            after_names = set()
            for after_stmt in body[i + 1:]:
                after_names.update(self._get_used_names(after_stmt))

            flagged = truly_unbound & after_names
            for var_name in sorted(flagged):
                self.violations.append(ConventionViolation(
                    rule="no_unbound_variables",
                    file=self._rel_path,
                    line=stmt.lineno,
                    message=(
                        f"Variable \'{var_name}\' is assigned only in the if-branch "
                        f"(line {stmt.lineno}) but used after — may be unbound if condition is False."
                    ),
                    severity="warning",
                    fix_hint=f"Initialize \'{var_name}\' before the if-statement or add an else-branch.",
                ))

    @staticmethod
    def _branch_always_exits(stmts: list[ast.stmt]) -> bool:
        """Check if a branch always exits (return/raise/continue/break)."""
        if not stmts:
            return False
        last = stmts[-1]
        if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
            return True
        # If last is an if/else that both exit
        if isinstance(last, ast.If) and last.orelse:
            return (
                _UnboundVariableVisitor._branch_always_exits(last.body)
                and _UnboundVariableVisitor._branch_always_exits(last.orelse)
            )
        return False

    def _get_assigned_names(self, stmts: list[ast.stmt]) -> set[str]:
        """Get all variable names assigned in a list of statements."""
        names: set[str] = set()
        for stmt in stmts:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
        return names

    def _get_used_names(self, node: ast.AST) -> set[str]:
        """Get all Name nodes (variable reads) in a subtree."""
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                names.add(child.id)
        return names


def _check_no_unbound_variables(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Find variables assigned only in if-branches but referenced after."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue

        visitor = _UnboundVariableVisitor(rel_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations




# ---------------------------------------------------------------------------
# Dead branches (Rule 5)
# ---------------------------------------------------------------------------


_EXIT_TYPES = (ast.Return, ast.Raise, ast.Continue, ast.Break)


class _DeadBranchVisitor(ast.NodeVisitor):
    """Find unreachable statements after unconditional exit in same block."""

    def __init__(self, rel_path: str) -> None:
        self.violations: list[ConventionViolation] = []
        self._rel_path = rel_path

    def _check_block(self, stmts: list[ast.stmt]) -> None:
        """Check a list of statements for dead code after exits."""
        for i, stmt in enumerate(stmts):
            if isinstance(stmt, _EXIT_TYPES) and i < len(stmts) - 1:
                # There are statements after this exit — they're dead
                dead_stmt = stmts[i + 1]
                self.violations.append(ConventionViolation(
                    rule="no_dead_branches",
                    file=self._rel_path,
                    line=dead_stmt.lineno,
                    message=(
                        f"Unreachable code at line {dead_stmt.lineno} — "
                        f"follows unconditional {type(stmt).__name__.lower()} at line {stmt.lineno}."
                    ),
                    severity="warning",
                    fix_hint="Remove dead code or restructure control flow.",
                ))
                break  # Only flag first dead statement per block

            # Recurse into sub-blocks
            if isinstance(stmt, (ast.If,)):
                self._check_block(stmt.body)
                self._check_block(stmt.orelse)
            elif isinstance(stmt, (ast.For, ast.While)):
                self._check_block(stmt.body)
                self._check_block(stmt.orelse)
            elif isinstance(stmt, ast.With):
                self._check_block(stmt.body)
            elif isinstance(stmt, ast.Try):
                self._check_block(stmt.body)
                for handler in stmt.handlers:
                    self._check_block(handler.body)
                self._check_block(stmt.orelse)
                self._check_block(stmt.finalbody)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._check_block(stmt.body)
            elif isinstance(stmt, ast.ClassDef):
                self._check_block(stmt.body)

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
        self._check_block(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check_block(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._check_block(node.body)
        self.generic_visit(node)


def _check_no_dead_branches(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Find unreachable code after unconditional exit statements."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue

        visitor = _DeadBranchVisitor(rel_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations


# ---------------------------------------------------------------------------
# List or single documented (Rule 6)
# ---------------------------------------------------------------------------


class _ListOrSingleVisitor(ast.NodeVisitor):
    """Find str-typed params used in iteration or .extend() without guard."""

    def __init__(self, rel_path: str) -> None:
        self.violations: list[ConventionViolation] = []
        self._rel_path = rel_path

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Check if any str-annotated params are used as iterables."""
        # Find params annotated as 'str'
        str_params: set[str] = set()
        for arg in func.args.args + func.args.kwonlyargs:
            if self._is_str_annotation(arg.annotation):
                str_params.add(arg.arg)

        if not str_params:
            return

        # Check if function has isinstance guard for these params
        guarded_params = self._find_isinstance_guards(func.body, str_params)

        # Find usage in iteration or .extend()
        unguarded = str_params - guarded_params
        if not unguarded:
            return

        for stmt in ast.walk(func):
            # for x in param (iterating a str gives characters — usually wrong)
            if isinstance(stmt, ast.For):
                if isinstance(stmt.iter, ast.Name) and stmt.iter.id in unguarded:
                    self.violations.append(ConventionViolation(
                        rule="list_or_single_documented",
                        file=self._rel_path,
                        line=stmt.lineno,
                        message=(
                            f"Parameter '{stmt.iter.id}' is typed 'str' but iterated in a for-loop — "
                            f"iterating a string gives individual characters, not items."
                        ),
                        severity="warning",
                        fix_hint=(
                            f"If '{stmt.iter.id}' can be a list, type it as 'str | list[str]' "
                            f"and add an isinstance guard."
                        ),
                    ))
            # .extend(param) — passing str to extend gives character-by-character
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if (isinstance(call.func, ast.Attribute)
                        and call.func.attr == "extend"
                        and call.args
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id in unguarded):
                    self.violations.append(ConventionViolation(
                        rule="list_or_single_documented",
                        file=self._rel_path,
                        line=stmt.lineno,
                        message=(
                            f"Parameter '{call.args[0].id}' is typed 'str' but passed to .extend() — "
                            f"extending with a string adds individual characters."
                        ),
                        severity="warning",
                        fix_hint=(
                            f"If '{call.args[0].id}' can be a list, type it as 'str | list[str]' "
                            f"and add an isinstance guard."
                        ),
                    ))

    @staticmethod
    def _is_str_annotation(ann: ast.expr | None) -> bool:
        """Check if an annotation is 'str'."""
        if ann is None:
            return False
        if isinstance(ann, ast.Name) and ann.id == "str":
            return True
        if isinstance(ann, ast.Constant) and ann.value == "str":
            return True
        return False

    @staticmethod
    def _find_isinstance_guards(
        body: list[ast.stmt], params: set[str]
    ) -> set[str]:
        """Find params protected by isinstance checks in the function body."""
        guarded: set[str] = set()
        for stmt in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(stmt, ast.Call):
                if (isinstance(stmt.func, ast.Name)
                        and stmt.func.id == "isinstance"
                        and len(stmt.args) >= 1
                        and isinstance(stmt.args[0], ast.Name)):
                    guarded.add(stmt.args[0].id)
        return guarded


def _check_list_or_single(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Find str-typed params used where list is likely intended."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=rel_path)
        except (OSError, SyntaxError):
            continue

        visitor = _ListOrSingleVisitor(rel_path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return violations


# ---------------------------------------------------------------------------
# Rule 7: Windows path separator — relative_to() without normalize
# ---------------------------------------------------------------------------

_RELATIVE_TO_RE = re.compile(
    r"""str\(\s*\w+\.relative_to\("""   # str(x.relative_to(
    r"""[^)]+\)\s*\)"""                 # ...))
    r"""(?!\s*\.replace)"""             # NOT followed by .replace
)


def _check_path_separator(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Flag str(path.relative_to(...)) without .replace('\\\\', '/')."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for i, line in enumerate(source.splitlines(), 1):
            if _RELATIVE_TO_RE.search(line):
                violations.append(ConventionViolation(
                    rule="path_separator",
                    file=rel_path,
                    line=i,
                    message="str(path.relative_to(...)) without .replace('\\\\', '/') — produces backslashes on Windows",
                    severity="warning",
                    fix_hint='Append .replace("\\\\", "/") to normalize path separators',
                ))

    return violations


# ---------------------------------------------------------------------------
# Rule 8: Missing encoding on write_text()
# ---------------------------------------------------------------------------

_WRITE_TEXT_NO_ENC_RE = re.compile(
    r"""\.write_text\([^)]*\)"""
)
_WRITE_TEXT_WITH_ENC_RE = re.compile(
    r"""\.write_text\([^)]*encoding\s*="""
)


def _check_write_text_encoding(
    changed_files: list[str], root: Path
) -> list[ConventionViolation]:
    """Flag .write_text() calls without explicit encoding= argument."""
    violations: list[ConventionViolation] = []

    for rel_path in changed_files:
        fpath = root / rel_path
        if not fpath.is_file() or not fpath.suffix == ".py":
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for i, line in enumerate(source.splitlines(), 1):
            if _WRITE_TEXT_NO_ENC_RE.search(line) and not _WRITE_TEXT_WITH_ENC_RE.search(line):
                violations.append(ConventionViolation(
                    rule="write_text_encoding",
                    file=rel_path,
                    line=i,
                    message='.write_text() without encoding="utf-8" — uses cp1252 on Windows',
                    severity="warning",
                    fix_hint='Add encoding="utf-8" argument',
                ))

    return violations


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

# Registry of all active rules
_RULES = [
    _check_no_hardcoded_paths,
    _check_no_deep_ternaries,
    _check_boundary_message_match,
    _check_no_unbound_variables,
    _check_no_dead_branches,
    _check_list_or_single,
    _check_path_separator,
    _check_write_text_encoding,
]


def run_anti_pattern_rules(
    changed_files: list[str],
    root: str,
) -> list[ConventionViolation]:
    """Run all anti-pattern rules against changed files.

    Args:
        changed_files: List of relative file paths to check.
        root: Project root directory (absolute path).

    Returns:
        List of violations found across all rules.
    """
    if not changed_files:
        return []

    root_path = Path(root).resolve()
    all_violations: list[ConventionViolation] = []

    for rule_fn in _RULES:
        all_violations.extend(rule_fn(changed_files, root_path))

    return all_violations
