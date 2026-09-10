"""odibi_anchor.codebase.semantic_edit_context — Intent-based code editing.

Accepts a semantic intent (add parameter, rename function, extract variable)
and produces a correct diff. Uses libcst when available for formatting-preserving
transforms; falls back to AST-based editing with stdlib.

Dependencies: libcst (optional), stdlib ast (fallback).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.ast_utils import ast_cache_invalidate
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

# Try to import libcst for high-quality transforms
try:
    import libcst as cst
    _HAS_LIBCST = True
except ImportError:
    _HAS_LIBCST = False
    cst = None  # type: ignore


# Supported actions
_SUPPORTED_ACTIONS = {
    "add_function",
    "add_parameter",
    "remove_parameter",
    "rename_parameter",
    "rename_function",
    "replace_function",
    "add_decorator",
    "remove_decorator",
    "change_return_type",
    "add_import",
    "remove_import",
}

_PATCH_LOG_FILE = ".patch_log.jsonl"


# Actions that require the target function to already exist in source
_FUNCTION_REQUIRED_ACTIONS = {
    "add_parameter", "remove_parameter", "rename_function",
    "rename_parameter", "add_decorator", "remove_decorator",
    "change_return_type", "replace_function",
}


def _function_exists_in_source(source: str, function_name: str, class_name: str | None = None) -> bool:
    """Check if a function exists in the source using AST parsing."""
    import ast as _ast
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return False  # Can't parse — don't block, let the transform handle it

    if class_name:
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        if item.name == function_name:
                            return True
        return False
    else:
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name == function_name:
                    return True
        return False


def semantic_edit_context(
    root: str | Path,
    *,
    target: str,
    action: str,
    function: str | None = None,
    class_name: str | None = None,
    param_name: str | None = None,
    param_type: str | None = None,
    param_default: str | None = None,
    param_keyword_only: bool = True,
    new_name: str | None = None,
    decorator: str | None = None,
    return_type: str | None = None,
    import_statement: str | None = None,
    params: str | None = None,
    body: str | None = None,
    apply: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Apply a semantic edit to a Python source file.

    Describes the edit as an intent (what to change) rather than a text
    replacement (how to change). Produces a diff preview by default;
    set apply=True to write the modified source to disk.
    """
    validate_output_format(output_format)

    if action not in _SUPPORTED_ACTIONS:
        raise ValueError(
            f"action must be one of {sorted(_SUPPORTED_ACTIONS)}, got {action!r}"
        )

    root = Path(root).resolve()
    target_path = root / target
    try:
        target_path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Target path escapes project root: {target}")
    if not target_path.exists():
        raise ValueError(f"target file not found: {target}")

    # Normalize decorator: strip leading @ if user included it
    if decorator and decorator.startswith("@"):
        decorator = decorator[1:]

    subject = subject or function or target_path.stem
    original_source = target_path.read_text(encoding="utf-8")

    # Choose engine
    engine = "libcst" if _HAS_LIBCST else "ast"

    # Apply transform
    try:
        if _HAS_LIBCST:
            modified_source = _apply_libcst_transform(
                original_source, action=action, function=function,
                class_name=class_name, param_name=param_name,
                param_type=param_type, param_default=param_default,
                param_keyword_only=param_keyword_only, new_name=new_name,
                decorator=decorator, return_type=return_type,
                import_statement=import_statement,
                params=params, body=body,
            )
        else:
            modified_source = _apply_ast_transform(
                original_source, action=action, function=function,
                class_name=class_name, param_name=param_name,
                param_type=param_type, param_default=param_default,
                param_keyword_only=param_keyword_only, new_name=new_name,
                decorator=decorator, return_type=return_type,
                import_statement=import_statement,
                params=params, body=body,
            )
        transform_error = None
    except Exception as exc:
        modified_source = original_source
        transform_error = str(exc)

    # Generate diff
    diff_lines = list(difflib.unified_diff(
        original_source.splitlines(),
        modified_source.splitlines(),
        fromfile=f"a/{target}",
        tofile=f"b/{target}",
        lineterm="",
    ))
    diff_preview = "\n".join(diff_lines)

    # Count changes
    additions = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    deletions = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    has_changes = additions > 0 or deletions > 0

    # Apply if requested and successful
    applied = False
    patch_record = None
    if apply and has_changes and not transform_error:
        target_path.write_text(modified_source, encoding="utf-8")
        ast_cache_invalidate(target_path)  # evict stale cached AST
        applied = True
        # Log the patch
        patch_record = _write_patch_record(
            root, target, action, original_source, modified_source, diff_preview,
        )

    # Detect function-not-found condition
    function_not_found = False
    if (
        function
        and action in _FUNCTION_REQUIRED_ACTIONS
        and not has_changes
        and not transform_error
    ):
        function_not_found = not _function_exists_in_source(
            original_source, function, class_name
        )

    # Build output
    metrics = {
        "engine": engine,
        "action": action,
        "target": target,
        "function": function,
        "lines_added": additions,
        "lines_removed": deletions,
        "has_changes": has_changes,
        "applied": applied,
        "has_error": transform_error is not None,
        "function_not_found": function_not_found,
        "patch_id": patch_record["patch_id"] if patch_record else None,
    }

    findings = []
    if has_changes:
        findings.append(f"Transform '{action}' produced {additions} addition(s), {deletions} deletion(s).")
        findings.append(f"Engine: {engine}.")
    elif transform_error:
        findings.append(f"Transform failed: {transform_error}")
    elif function_not_found:
        findings.append(
            f"WARNING: Function '{function}' not found in {target}"
            + (f" (class {class_name})" if class_name else "")
            + ". No changes possible."
        )
    else:
        findings.append("No changes produced — target may already match the desired state.")

    if applied:
        findings.append(f"Changes written to {target}.")
    if patch_record:
        findings.append(f"Patch logged: {patch_record['patch_id']} (rollback: check .patch_log.jsonl)")

    risks = []
    if function_not_found:
        risks.append(
            f"Target function '{function}' does not exist in {target}. "
            f"Verify the function name and class_name (if applicable)."
        )
    if transform_error:
        risks.append(f"Transform error: {transform_error}")
    if not _HAS_LIBCST:
        risks.append(
            "Using stdlib ast fallback — comments and formatting may not be preserved. "
            "Install libcst for formatting-preserving transforms: pip install libcst"
        )
    if applied:
        risks.append("Changes applied to disk. Run preflight_context to verify correctness.")

    suggested_actions = []
    if has_changes and not applied:
        suggested_actions.append("MUST: Review diff_preview before applying.")
        suggested_actions.append(
            "MUST: Apply with semantic_edit_context(..., apply=True) when satisfied."
        )
        suggested_actions.append(
            "MUST: Run known_bad_change_context(root, changed_files=[target], action=action) before applying."
        )
    if applied:
        suggested_actions.append("MUST: Run anchor('preflight', changed_files=[...]) to check for type errors.")
        suggested_actions.append("MUST: Run anchor('test', changed_files=[...]) to identify affected tests.")
        suggested_actions.append("MUST: Run anchor('touched', 'path') to register this change.")
        suggested_actions.append("MUST: After gate, use anchor('learning', 'capture|assess', ...) with real Observations or a truthful nothing_reusable_learned assessment.")

    summary = (
        f"{action} on {subject}: {additions} addition(s), {deletions} deletion(s)"
        f"{' (APPLIED)' if applied else ' (dry-run)'}"
        f"{' — ERROR: ' + transform_error if transform_error else ''}"
    )

    ctx = build_base_context(
        kind="semantic_edit_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        diff_preview=diff_preview,
        modified_source=modified_source if has_changes else "",
        transform_error=transform_error,
        patch_id=patch_record["patch_id"] if patch_record else None,
    )

    if output_format == "markdown":
        return render_semantic_edit_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Patch Logging
# ---------------------------------------------------------------------------


def _write_patch_record(
    root: Path,
    target: str,
    action: str,
    original_source: str,
    modified_source: str,
    diff_preview: str,
) -> dict[str, Any]:
    """Write a patch record to the JSONL log after apply=True."""
    patch_id = f"p-{uuid.uuid4().hex[:12]}"
    record = {
        "patch_id": patch_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": action,
        "target": target,
        "before_hash": hashlib.sha256(original_source.encode()).hexdigest()[:16],
        "after_hash": hashlib.sha256(modified_source.encode()).hexdigest()[:16],
        "diff": diff_preview,
    }

    patch_log = root / _PATCH_LOG_FILE
    with open(patch_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


# ---------------------------------------------------------------------------
# libcst Transforms
# ---------------------------------------------------------------------------


def _apply_libcst_transform(
    source: str,
    *,
    action: str,
    function: str | None,
    class_name: str | None,
    param_name: str | None,
    param_type: str | None,
    param_default: str | None,
    param_keyword_only: bool,
    new_name: str | None,
    decorator: str | None,
    return_type: str | None,
    import_statement: str | None,
    params: str | None = None,
    body: str | None = None,
) -> str:
    """Apply a transform using libcst (formatting-preserving)."""
    if not _HAS_LIBCST:
        raise ImportError("libcst is required for this transform")

    tree = cst.parse_module(source)

    if action == "add_function":
        if not function:
            raise ValueError("add_function requires function (the function name)")
        transformer = _AddFunctionTransformer(
            function, params or "", body or "pass", decorator, class_name,
        )
        new_tree = tree.visit(transformer)
        return new_tree.code

    elif action == "add_parameter":
        if not function or not param_name:
            raise ValueError("add_parameter requires function and param_name")
        transformer = _AddParameterTransformer(
            function, param_name, param_type, param_default,
            param_keyword_only, class_name,
        )
    elif action == "remove_parameter":
        if not function or not param_name:
            raise ValueError("remove_parameter requires function and param_name")
        transformer = _RemoveParameterTransformer(function, param_name, class_name)
    elif action == "rename_function":
        if not function or not new_name:
            raise ValueError("rename_function requires function and new_name")
        transformer = _RenameFunctionTransformer(function, new_name, class_name)
    elif action == "rename_parameter":
        if not function or not param_name or not new_name:
            raise ValueError("rename_parameter requires function, param_name, and new_name")
        transformer = _RenameParameterTransformer(function, param_name, new_name, class_name)
    elif action == "add_import":
        if not import_statement:
            raise ValueError("add_import requires import_statement")
        transformer = _AddImportTransformer(import_statement)
    elif action == "remove_import":
        if not import_statement:
            raise ValueError("remove_import requires import_statement")
        transformer = _RemoveImportTransformer(import_statement)
    elif action == "add_decorator":
        if not function or not decorator:
            raise ValueError("add_decorator requires function and decorator")
        transformer = _AddDecoratorTransformer(function, decorator, class_name)
    elif action == "remove_decorator":
        if not function or not decorator:
            raise ValueError("remove_decorator requires function and decorator")
        transformer = _RemoveDecoratorTransformer(function, decorator, class_name)
    elif action == "change_return_type":
        if not function or not return_type:
            raise ValueError("change_return_type requires function and return_type")
        transformer = _ChangeReturnTypeTransformer(function, return_type, class_name)
    elif action == "replace_function":
        if not function or not body:
            raise ValueError("replace_function requires function and body")
        transformer = _ReplaceFunctionBodyTransformer(function, body, class_name)
    else:
        raise ValueError(f"libcst transform not implemented for action: {action}")

    new_tree = tree.visit(transformer)
    return new_tree.code


# ---------------------------------------------------------------------------
# libcst transformer classes (conditional on import)
# ---------------------------------------------------------------------------

if _HAS_LIBCST:

    class _AddParameterTransformer(cst.CSTTransformer):
        def __init__(self, func_name, param_name, param_type, param_default,
                     keyword_only, class_name):
            self.func_name = func_name
            self.param_name = param_name
            self.param_type = param_type
            self.param_default = param_default
            self.keyword_only = keyword_only
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node

            annotation = None
            if self.param_type:
                annotation = cst.Annotation(annotation=cst.parse_expression(self.param_type))

            default = None
            if self.param_default:
                default = cst.parse_expression(self.param_default)

            new_param = cst.Param(
                name=cst.Name(self.param_name),
                annotation=annotation,
                default=default,
            )

            params = updated_node.params

            if self.keyword_only:
                if isinstance(params.star_arg, cst.ParamStar) or params.kwonly_params:
                    existing_kwonly = list(params.kwonly_params)
                    # Fix formatting: swap commas so new param gets its own line
                    if existing_kwonly:
                        last = existing_kwonly[-1]
                        # Find a mid-param comma (has indented last_line for next param)
                        mid_comma = None
                        for p in existing_kwonly[:-1]:
                            if p.comma and not isinstance(p.comma, cst.MaybeSentinel):
                                mid_comma = p.comma
                                break
                        if mid_comma and last.comma and not isinstance(last.comma, cst.MaybeSentinel):
                            # Swap: give old-last a mid-style comma, give new param
                            # the old-last's comma (which leads to closing paren)
                            new_param = new_param.with_changes(comma=last.comma)
                            existing_kwonly[-1] = last.with_changes(comma=mid_comma)
                        elif not last.comma or isinstance(last.comma, cst.MaybeSentinel):
                            # Last param has no comma at all — add one
                            if mid_comma:
                                existing_kwonly[-1] = last.with_changes(comma=mid_comma)
                            else:
                                existing_kwonly[-1] = last.with_changes(
                                    comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                                )
                    new_kwonly = existing_kwonly + [new_param]
                    params = params.with_changes(kwonly_params=new_kwonly)
                else:
                    params = params.with_changes(
                        star_arg=cst.ParamStar(),
                        kwonly_params=[new_param],
                    )
            else:
                existing_params = list(params.params)
                # Same comma-swap fix for positional params
                if existing_params:
                    last = existing_params[-1]
                    mid_comma = None
                    for p in existing_params[:-1]:
                        if p.comma and not isinstance(p.comma, cst.MaybeSentinel):
                            mid_comma = p.comma
                            break
                    if mid_comma and last.comma and not isinstance(last.comma, cst.MaybeSentinel):
                        new_param = new_param.with_changes(comma=last.comma)
                        existing_params[-1] = last.with_changes(comma=mid_comma)
                    elif not last.comma or isinstance(last.comma, cst.MaybeSentinel):
                        if mid_comma:
                            existing_params[-1] = last.with_changes(comma=mid_comma)
                        else:
                            existing_params[-1] = last.with_changes(
                                comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
                            )
                new_params = existing_params + [new_param]
                params = params.with_changes(params=new_params)

            return updated_node.with_changes(params=params)


    class _AddFunctionTransformer(cst.CSTTransformer):
        """Insert a new function definition at the end of a module (or class)."""

        def __init__(self, func_name, params_str, body_str, decorator, class_name):
            self.func_name = func_name
            self.params_str = params_str
            self.body_str = body_str
            self.decorator = decorator
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            if self.class_name and original_node.name.value == self.class_name:
                # Add function inside this class
                new_func = self._build_function_node(indented=True)
                new_body = updated_node.body.with_changes(
                    body=list(updated_node.body.body) + [new_func]
                )
                self._in_target_class = False
                return updated_node.with_changes(body=new_body)
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_Module(self, original_node, updated_node):
            if self.class_name:
                # Already handled in leave_ClassDef
                return updated_node
            new_func = self._build_function_node(indented=False)
            new_body = list(updated_node.body) + [new_func]
            return updated_node.with_changes(body=new_body)

        def _build_function_node(self, indented: bool):
            """Build a FunctionDef CST node from the stored parameters."""
            # Always build at indent=0 — CST handles indentation when inserted into class body
            body_indent = "    "

            # Build parameter list
            if self.params_str:
                sig = f"def {self.func_name}({self.params_str}):"
            else:
                sig = f"def {self.func_name}():"

            # Build body lines
            body_lines = self.body_str.split("\n")
            body_src = "\n".join(f"{body_indent}{line}" for line in body_lines)

            # Build decorator
            dec_line = ""
            if self.decorator:
                dec_name = self.decorator.lstrip("@")
                dec_line = f"@{dec_name}\n"

            full_src = f"\n\n{dec_line}{sig}\n{body_src}\n"

            # Parse as a module and extract the FunctionDef
            parsed = cst.parse_module(full_src)
            for stmt in parsed.body:
                if isinstance(stmt, cst.FunctionDef):
                    return stmt
            # Fallback: return the parsed statement directly
            return parsed.body[-1]

    class _RemoveParameterTransformer(cst.CSTTransformer):
        def __init__(self, func_name, param_name, class_name):
            self.func_name = func_name
            self.param_name = param_name
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node
            params = updated_node.params
            new_params = [p for p in params.params if p.name.value != self.param_name]
            new_kwonly = [p for p in params.kwonly_params if p.name.value != self.param_name]
            params = params.with_changes(params=new_params, kwonly_params=new_kwonly)
            return updated_node.with_changes(params=params)

    class _RenameFunctionTransformer(cst.CSTTransformer):
        def __init__(self, old_name, new_name, class_name):
            self.old_name = old_name
            self.new_name = new_name
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value == self.old_name:
                return updated_node.with_changes(name=cst.Name(self.new_name))
            return updated_node

        def leave_Name(self, original_node, updated_node):
            if updated_node.value == self.old_name:
                return updated_node.with_changes(value=self.new_name)
            return updated_node

    class _RenameParameterTransformer(cst.CSTTransformer):
        def __init__(self, func_name, old_name, new_name, class_name):
            self.func_name = func_name
            self.old_name = old_name
            self.new_name = new_name
            self.class_name = class_name
            self._in_target_class = class_name is None
            self._in_target_func = False

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def visit_FunctionDef(self, node):
            if self._in_target_class and node.name.value == self.func_name:
                self._in_target_func = True
            return True

        def leave_FunctionDef(self, original_node, updated_node):
            self._in_target_func = False
            return updated_node

        def leave_Param(self, original_node, updated_node):
            if self._in_target_func and updated_node.name.value == self.old_name:
                return updated_node.with_changes(name=cst.Name(self.new_name))
            return updated_node

    class _AddImportTransformer(cst.CSTTransformer):
        def __init__(self, import_statement):
            self.import_statement = import_statement
            self._added = False

        def leave_Module(self, original_node, updated_node):
            if self._added:
                return updated_node
            import_node = cst.parse_statement(self.import_statement)
            new_body = list(updated_node.body)
            last_import_idx = -1
            for i, stmt in enumerate(new_body):
                if isinstance(stmt, cst.SimpleStatementLine):
                    for child in stmt.body:
                        if isinstance(child, (cst.Import, cst.ImportFrom)):
                            last_import_idx = i
            insert_idx = last_import_idx + 1 if last_import_idx >= 0 else 0
            new_body.insert(insert_idx, import_node)
            self._added = True
            return updated_node.with_changes(body=new_body)

    class _RemoveImportTransformer(cst.CSTTransformer):
        def __init__(self, import_statement):
            self.match_text = import_statement.strip()

        def leave_SimpleStatementLine(self, original_node, updated_node):
            try:
                line_code = cst.parse_module("").code_for_node(original_node).strip()
            except Exception:
                line_code = ""  # SILENT-OK: CST code_for_node failure — use empty fallback
            if self.match_text in line_code:
                return cst.RemoveFromParent()
            return updated_node

    class _AddDecoratorTransformer(cst.CSTTransformer):
        def __init__(self, func_name, decorator, class_name):
            self.func_name = func_name
            self.decorator = decorator
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node
            new_decorator = cst.Decorator(decorator=cst.parse_expression(self.decorator))
            new_decorators = list(updated_node.decorators) + [new_decorator]
            return updated_node.with_changes(decorators=new_decorators)

    class _RemoveDecoratorTransformer(cst.CSTTransformer):
        def __init__(self, func_name, decorator, class_name):
            self.func_name = func_name
            self.decorator_name = decorator
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node
            new_decorators = []
            for d in updated_node.decorators:
                try:
                    name = cst.parse_module("").code_for_node(d.decorator).strip()
                except Exception:
                    name = ""  # SILENT-OK: CST code_for_node failure — use empty fallback
                if self.decorator_name not in name:
                    new_decorators.append(d)
            return updated_node.with_changes(decorators=new_decorators)

    class _ChangeReturnTypeTransformer(cst.CSTTransformer):
        def __init__(self, func_name, return_type, class_name):
            self.func_name = func_name
            self.return_type = return_type
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node
            new_annotation = cst.Annotation(annotation=cst.parse_expression(self.return_type))
            return updated_node.with_changes(returns=new_annotation)



    class _ReplaceFunctionBodyTransformer(cst.CSTTransformer):
        """Replace the body of a named function with new code."""

        def __init__(self, func_name, body_str, class_name):
            self.func_name = func_name
            self.body_str = body_str
            self.class_name = class_name
            self._in_target_class = class_name is None

        def visit_ClassDef(self, node):
            if self.class_name and node.name.value == self.class_name:
                self._in_target_class = True
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self._in_target_class = self.class_name is None
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            if not self._in_target_class:
                return updated_node
            if updated_node.name.value != self.func_name:
                return updated_node
            # Build the replacement body at indent=0, let CST handle placement
            body_lines = self.body_str.split("\n")
            body_src = "\n".join(
                f"    {line}" if line.strip() else "" for line in body_lines
            )
            full_src = f"def _placeholder():\n{body_src}\n"
            parsed = cst.parse_module(full_src)
            new_func = None
            for stmt in parsed.body:
                if isinstance(stmt, cst.FunctionDef):
                    new_func = stmt
                    break
            if new_func is None:
                return updated_node
            return updated_node.with_changes(body=new_func.body)


# ---------------------------------------------------------------------------
# stdlib ast Fallback Transforms
# ---------------------------------------------------------------------------


def _apply_ast_transform(
    source: str,
    *,
    action: str,
    function: str | None,
    class_name: str | None,
    param_name: str | None,
    param_type: str | None,
    param_default: str | None,
    param_keyword_only: bool,
    new_name: str | None,
    decorator: str | None,
    return_type: str | None,
    import_statement: str | None,
    params: str | None = None,
    body: str | None = None,
) -> str:
    """Apply transform using stdlib ast (lossy — may alter formatting).

    Only supports a subset of actions. Raises ValueError for unsupported ones.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)

    if action == "add_function" and function:
        return _ast_add_function(lines, function, params or "", body or "pass", decorator)
    elif action == "add_parameter" and function:
        return _ast_add_parameter(lines, tree, function, class_name,
                                   param_name, param_type, param_default,
                                   param_keyword_only)
    elif action == "rename_function" and function and new_name:
        return _ast_rename_function(lines, tree, function, new_name, class_name)
    elif action == "add_import" and import_statement:
        return _ast_add_import(lines, tree, import_statement)
    elif action == "remove_import" and import_statement:
        return _ast_remove_import(lines, import_statement)
    elif action == "replace_function" and function and body:
        return _ast_replace_function(lines, tree, function, body, class_name)
    else:
        raise ValueError(
            f"stdlib ast fallback does not support action '{action}'. "
            f"Install libcst for full support: pip install libcst"
        )


def _ast_add_parameter(lines, tree, function, class_name,
                        param_name, param_type, param_default, keyword_only):
    """Add a parameter by finding the function def line and modifying it."""
    func_node = _find_function_node(tree, function, class_name)
    if not func_node:
        raise ValueError(f"Function {function!r} not found")

    # Build parameter string
    param_str = param_name or ""
    if param_type:
        param_str += f": {param_type}"
    if param_default:
        param_str += f" = {param_default}"

    # Find the closing paren of the function definition
    def_start = func_node.lineno - 1
    paren_depth = 0
    found_open = False

    for i in range(def_start, min(def_start + 50, len(lines))):
        line = lines[i]
        for j, char in enumerate(line):
            if char == "(":
                paren_depth += 1
                found_open = True
            elif char == ")":
                paren_depth -= 1
                if found_open and paren_depth == 0:
                    insert_line = i
                    insert_col = j

                    if keyword_only:
                        func_source = "".join(lines[def_start:i+1])
                        if "*" in func_source and "**" not in func_source.split("*")[0]:
                            insert_text = f", {param_str}"
                        else:
                            insert_text = f", *, {param_str}"
                    else:
                        insert_text = f", {param_str}"

                    line_content = lines[insert_line]
                    lines[insert_line] = (
                        line_content[:insert_col] +
                        insert_text +
                        line_content[insert_col:]
                    )
                    return "".join(lines)

    raise ValueError(f"Could not find closing paren for function {function!r}")



def _ast_add_function(lines, function, params_str, body_str, decorator):
    """Add a function at the end of the module (ast fallback)."""
    result = "".join(lines).rstrip()

    # Build function source
    func_lines = []
    func_lines.append("")
    func_lines.append("")
    if decorator:
        dec_name = decorator.lstrip("@")
        func_lines.append(f"@{dec_name}")
    if params_str:
        func_lines.append(f"def {function}({params_str}):")
    else:
        func_lines.append(f"def {function}():")

    # Body
    for body_line in body_str.split("\n"):
        func_lines.append(f"    {body_line}")
    func_lines.append("")

    result += "\n".join(func_lines)
    return result


def _ast_rename_function(lines, tree, old_name, new_name, class_name):
    """Rename function by simple text replacement."""
    import re as _re
    result = "".join(lines)
    result = _re.sub(
        rf"\bdef {_re.escape(old_name)}\s*\(",
        f"def {new_name}(",
        result,
    )
    result = _re.sub(
        rf"\b{_re.escape(old_name)}\b",
        new_name,
        result,
    )
    return result


def _ast_add_import(lines, tree, import_statement):
    """Add an import statement after the last existing import."""
    last_import_line = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_line = max(last_import_line, node.lineno)

    insert_at = last_import_line  # After last import
    lines.insert(insert_at, import_statement.rstrip() + "\n")
    return "".join(lines)


def _ast_remove_import(lines, import_statement):
    """Remove an import statement by line matching."""
    match_text = import_statement.strip()
    result_lines = [
        l for l in lines
        if match_text not in l.strip()
    ]
    return "".join(result_lines)


def _ast_replace_function(lines, tree, function, body_str, class_name):
    """Replace function body using AST line-based approach (fallback)."""
    func_node = _find_function_node(tree, function, class_name)
    if not func_node:
        raise ValueError(f"Function {function!r} not found")

    # Find body start (line after the function def + docstring)
    body_start = func_node.body[0].lineno - 1  # 0-indexed
    body_end = func_node.end_lineno  # 1-indexed, inclusive

    # Determine indentation from original body
    indent = ""
    for char in lines[body_start]:
        if char in (" ", "\t"):
            indent += char
        else:
            break

    # Build replacement body
    new_body_lines = []
    for line in body_str.split("\n"):
        new_body_lines.append(f"{indent}{line}\n" if line.strip() else "\n")

    # Replace lines
    result_lines = lines[:body_start] + new_body_lines + lines[body_end:]
    return "".join(result_lines)


def _find_function_node(tree, function, class_name):
    """Find a FunctionDef node by name, optionally within a class."""
    if class_name:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for child in ast.walk(node):
                    if isinstance(child, ast.FunctionDef) and child.name == function:
                        return child
    else:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == function:
                return node
    return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_semantic_edit_report(ctx: dict[str, Any]) -> str:
    """Render semantic_edit_context output as markdown."""
    lines = render_header_lines(ctx, "Semantic Edit")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx.get("diff_preview"):
        lines.extend(["", "## Diff Preview", "", "```diff",
                       ctx["diff_preview"], "```"])

    if ctx.get("transform_error"):
        lines.extend(["", "## Transform Error", "",
                       f"```\n{ctx['transform_error']}\n```"])

    lines.extend(render_bullet_section(ctx["findings"], "## Findings"))
    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Next Actions"))

    return "\n".join(lines)
