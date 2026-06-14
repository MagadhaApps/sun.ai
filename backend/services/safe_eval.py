"""
Safe AST-walking expression evaluator.

Replaces direct eval() calls with a restricted interpreter that only
supports a whitelisted set of AST node types. No arbitrary code execution.
"""

import ast
import operator
import math
import json as _json

# ── node-type allowlist ──────────────────────────────────────────────
_SAFE_AST_NODES = frozenset({
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.Call, ast.Attribute, ast.Subscript,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.DictComp,
    ast.SetComp, ast.comprehension, ast.Slice,
    ast.IfExp, ast.Num, ast.Str, ast.Bytes, ast.NameConstant,
    ast.JoinedStr, ast.FormattedValue, ast.keyword, ast.arg,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.MatMult, ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd,
    ast.And, ast.Or, ast.Not, ast.Invert, ast.UAdd, ast.USub,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
})

# ── operator lookup ──────────────────────────────────────────────────
_BIN_OPS = {
    ast.Add:      operator.add,
    ast.Sub:      operator.sub,
    ast.Mult:     operator.mul,
    ast.Div:      operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod:      operator.mod,
    ast.Pow:      operator.pow,
    ast.LShift:   operator.lshift,
    ast.RShift:   operator.rshift,
    ast.BitOr:    operator.or_,
    ast.BitXor:   operator.xor,
    ast.BitAnd:   operator.and_,
    ast.MatMult:  operator.matmul,
}

_UNARY_OPS = {
    ast.Not:    operator.not_,
    ast.Invert: operator.invert,
    ast.UAdd:   operator.pos,
    ast.USub:   operator.neg,
}

_CMP_OPS = {
    ast.Eq:    operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt:    operator.lt,
    ast.LtE:   operator.le,
    ast.Gt:    operator.gt,
    ast.GtE:   operator.ge,
    ast.Is:    operator.is_,
    ast.IsNot: operator.is_not,
    ast.In:    lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

# ── public API ───────────────────────────────────────────────────────

def safe_eval(expression: str, globals_dict: dict = None, locals_dict: dict = None):
    """Evaluate *expression* using a restricted AST interpreter.

    Parameters
    ----------
    expression : str
        A Python *expression* (not statements).  Must pass AST validation.
    globals_dict : dict | None
        Names accessible as globals (e.g. ``{"__builtins__": {...}}``).
    locals_dict : dict | None
        Names accessible as locals (e.g. ``{"data": ...}``).

    Returns
    -------
    The result of evaluating the expression.

    Raises
    ------
    ValueError
        If the expression contains a forbidden AST node or accesses private
        attributes.
    """
    tree = _validate_and_parse(expression)
    merged = _merge_scopes(globals_dict, locals_dict)
    return _eval_node(tree.body, merged)


# ── internals ────────────────────────────────────────────────────────

def _validate_and_parse(expression: str) -> ast.Expression:
    """Parse and validate the expression AST."""
    tree = ast.parse(expression, mode='eval')
    for node in ast.walk(tree):
        if type(node) not in _SAFE_AST_NODES:
            raise ValueError(
                f"Unsafe expression: forbidden construct {type(node).__name__}"
            )
        # Block attribute access to dunder / private attributes
        if isinstance(node, ast.Attribute) and isinstance(node.attr, str):
            if node.attr.startswith('_'):
                raise ValueError(
                    "Unsafe expression: access to private attributes is forbidden"
                )
    return tree


def _merge_scopes(globals_dict: dict, locals_dict: dict) -> dict:
    """Merge globals and locals into a single lookup dict (locals win)."""
    scope = {}
    if globals_dict:
        scope.update(globals_dict)
    if locals_dict:
        scope.update(locals_dict)
    return scope


def _eval_node(node, scope: dict):
    """Recursively evaluate a single AST node."""
    # ── literals ─────────────────────────────────────────────────
    if isinstance(node, ast.Constant):
        return node.value
    # Fallback for Python < 3.8
    if isinstance(node, (ast.Num, ast.Str, ast.Bytes, ast.NameConstant)):
        return node.n if isinstance(node, ast.Num) else node.s if isinstance(node, (ast.Str, ast.Bytes)) else node.value

    # ── name lookups ─────────────────────────────────────────────
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        # Fallback to __builtins__ (matching Python eval semantics)
        builtins = scope.get("__builtins__")
        if isinstance(builtins, dict) and node.id in builtins:
            return builtins[node.id]
        raise ValueError(f"Unknown name: {node.id}")

    # ── binary operators ─────────────────────────────────────────
    if isinstance(node, ast.BinOp):
        op_func = _BIN_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported binary operator: {type(node.op).__name__}")
        return op_func(_eval_node(node.left, scope), _eval_node(node.right, scope))

    # ── unary operators ──────────────────────────────────────────
    if isinstance(node, ast.UnaryOp):
        op_func = _UNARY_OPS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op_func(_eval_node(node.operand, scope))

    # ── boolean operators (and / or) ─────────────────────────────
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result = True
            for val in (_eval_node(v, scope) for v in node.values):
                result = result and val
                if not result:
                    break
            return result
        elif isinstance(node.op, ast.Or):
            result = False
            for val in (_eval_node(v, scope) for v in node.values):
                result = result or val
                if result:
                    break
            return result
        raise ValueError(f"Unsupported boolean operator: {type(node.op).__name__}")

    # ── comparisons ──────────────────────────────────────────────
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, scope)
        for op_node, comp_node in zip(node.ops, node.comparators):
            cmp_func = _CMP_OPS.get(type(op_node))
            if cmp_func is None:
                raise ValueError(f"Unsupported comparison: {type(op_node).__name__}")
            right = _eval_node(comp_node, scope)
            if not cmp_func(left, right):
                return False
            left = right
        return True

    # ── function calls ───────────────────────────────────────────
    if isinstance(node, ast.Call):
        func = _eval_node(node.func, scope)
        args = [_eval_node(a, scope) for a in node.args]
        kwargs = {kw.arg: _eval_node(kw.value, scope) for kw in node.keywords}
        if not callable(func):
            raise ValueError(f"Object is not callable: {type(func).__name__}")
        return func(*args, **kwargs)

    # ── attribute access ─────────────────────────────────────────
    if isinstance(node, ast.Attribute):
        obj = _eval_node(node.value, scope)
        if isinstance(node.attr, str) and node.attr.startswith('_'):
            raise ValueError("Unsafe expression: access to private attributes is forbidden")
        return getattr(obj, node.attr)

    # ── subscript (index / slice) ────────────────────────────────
    if isinstance(node, ast.Subscript):
        obj = _eval_node(node.value, scope)
        if isinstance(node.slice, ast.Slice):
            lower = _eval_node(node.slice.lower, scope) if node.slice.lower else None
            upper = _eval_node(node.slice.upper, scope) if node.slice.upper else None
            step = _eval_node(node.slice.step, scope) if node.slice.step else None
            return obj[slice(lower, upper, step)]
        idx = _eval_node(node.slice, scope)
        return obj[idx]

    # ── container literals ───────────────────────────────────────
    if isinstance(node, ast.List):
        return [_eval_node(el, scope) for el in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(el, scope) for el in node.elts)

    if isinstance(node, ast.Set):
        return {_eval_node(el, scope) for el in node.elts}

    if isinstance(node, ast.Dict):
        return {
            _eval_node(k, scope): _eval_node(v, scope)
            for k, v in zip(node.keys, node.values)
        }

    # ── comprehensions ───────────────────────────────────────────
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
        return _eval_comprehension(node, scope)

    # ── ternary (if-else expression) ─────────────────────────────
    if isinstance(node, ast.IfExp):
        if _eval_node(node.test, scope):
            return _eval_node(node.body, scope)
        return _eval_node(node.orelse, scope)

    # ── f-strings ────────────────────────────────────────────────
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(v.value if isinstance(v.value, str) else str(v.value))
            elif isinstance(v, ast.FormattedValue):
                val = _eval_node(v.value, scope)
                if v.format_spec:
                    fmt = _eval_node(v.format_spec, scope)
                    parts.append(format(val, fmt))
                else:
                    parts.append(str(val))
            else:
                parts.append(str(_eval_node(v, scope)))
        return "".join(parts)

    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def _eval_comprehension(node, scope: dict):
    """Evaluate a list / set / dict comprehension."""

    def _evaluate_generators(generators, outer_scope: dict):
        """Recursively process nested generators and collect results."""
        if not generators:
            return [({}, outer_scope)]

        gen = generators[0]
        rest = generators[1:]
        iter_obj = _eval_node(gen.iter, outer_scope)
        results = []

        for item in iter_obj:
            inner_scope = dict(outer_scope)
            if isinstance(gen.target, ast.Name):
                inner_scope[gen.target.id] = item
            elif isinstance(gen.target, ast.Tuple):
                # handle tuple unpacking in comprehensions
                item_seq = list(item) if hasattr(item, '__iter__') and not isinstance(item, str) else [item]
                for i, elt in enumerate(gen.target.elts):
                    if isinstance(elt, ast.Name) and i < len(item_seq):
                        inner_scope[elt.id] = item_seq[i]
            else:
                raise ValueError(f"Unsupported comprehension target: {type(gen.target).__name__}")

            # Check if-condition
            if gen.ifs:
                ifs_ok = True
                for if_clause in gen.ifs:
                    if not _eval_node(if_clause, inner_scope):
                        ifs_ok = False
                        break
                if not ifs_ok:
                    continue

            if rest:
                results.extend(_evaluate_generators(rest, inner_scope))
            else:
                results.append(({}, inner_scope))

        return results

    # For list/set comprehensions
    if isinstance(node, ast.ListComp):
        results = []
        for _, inner_scope in _evaluate_generators(node.generators, scope):
            results.append(_eval_node(node.elt, inner_scope))
        return results

    if isinstance(node, ast.SetComp):
        results = set()
        for _, inner_scope in _evaluate_generators(node.generators, scope):
            results.add(_eval_node(node.elt, inner_scope))
        return results

    if isinstance(node, ast.DictComp):
        results = {}
        for _, inner_scope in _evaluate_generators(node.generators, scope):
            k = _eval_node(node.key, inner_scope)
            v = _eval_node(node.value, inner_scope)
            results[k] = v
        return results

    raise ValueError(f"Unsupported comprehension type: {type(node).__name__}")
