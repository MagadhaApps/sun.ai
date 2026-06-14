import json
import uuid
import time
import ast
import logging
from datetime import datetime
from database import get_db
from services.chat_service import non_stream_chat_completion
from services.tool_service import execute_tool
from services.mcp_service import execute_mcp_tool
from services.agent_service import run_agent

logger = logging.getLogger(__name__)

# Safe AST node types for expression evaluation
_SAFE_AST_NODES = {
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
    ast.In, ast.NotIn
}

def _validate_expression_ast(expression: str):
    """Validate that an expression AST contains only safe node types and no private attribute access."""
    tree = ast.parse(expression, mode='eval')
    for node in ast.walk(tree):
        if type(node) not in _SAFE_AST_NODES:
            raise ValueError(f"Unsafe expression: forbidden construct {type(node).__name__}")
        # Block attribute access to dunder methods (e.g., __class__, __bases__)
        if isinstance(node, ast.Attribute) and isinstance(node.attr, str) and node.attr.startswith('_'):
            raise ValueError("Unsafe expression: access to private attributes is forbidden")


def _safe_eval_expression(expression: str, safe_builtins: dict, local_vars: dict):
    """Evaluate an expression via AST walking — no exec/eval used.
    The expression AST must pass _validate_expression_ast first."""
    tree = ast.parse(expression, mode='eval')
    _validate_expression_ast(expression)

    def _eval_node(node, loc):
        """Recursively evaluate a safe AST node."""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.Bytes):
            return node.s
        if isinstance(node, ast.NameConstant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in loc:
                return loc[node.id]
            if node.id in safe_builtins:
                return safe_builtins[node.id]
            raise NameError(f"name '{node.id}' is not defined")
        if isinstance(node, ast.List):
            return [_eval_node(el, loc) for el in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(_eval_node(el, loc) for el in node.elts)
        if isinstance(node, ast.Dict):
            return {_eval_node(k, loc): _eval_node(v, loc) for k, v in zip(node.keys, node.values)}
        if isinstance(node, ast.Set):
            return {_eval_node(el, loc) for el in node.elts}
        if isinstance(node, ast.Subscript):
            obj = _eval_node(node.value, loc)
            slc = _eval_node(node.slice, loc)
            return obj[slc]
        if isinstance(node, ast.Index):
            return _eval_node(node.value, loc)
        if isinstance(node, ast.Slice):
            lower = _eval_node(node.lower, loc) if node.lower else None
            upper = _eval_node(node.upper, loc) if node.upper else None
            step = _eval_node(node.step, loc) if node.step else None
            return slice(lower, upper, step)
        if isinstance(node, ast.Attribute):
            obj = _eval_node(node.value, loc)
            return getattr(obj, node.attr)
        if isinstance(node, ast.Call):
            func = _eval_node(node.func, loc)
            args = [_eval_node(a, loc) for a in node.args]
            kwargs = {kw.arg: _eval_node(kw.value, loc) for kw in node.keywords}
            return func(*args, **kwargs)
        if isinstance(node, ast.BinOp):
            left = _eval_node(node.left, loc)
            right = _eval_node(node.right, loc)
            op_map = {
                ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
                ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
                ast.FloorDiv: lambda a, b: a // b, ast.MatMult: lambda a, b: a @ b,
                ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
                ast.BitOr: lambda a, b: a | b, ast.BitXor: lambda a, b: a ^ b,
                ast.BitAnd: lambda a, b: a & b,
            }
            return op_map[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            operand = _eval_node(node.operand, loc)
            op_map = {
                ast.Not: lambda a: not a, ast.Invert: lambda a: ~a,
                ast.UAdd: lambda a: +a, ast.USub: lambda a: -a,
            }
            return op_map[type(node.op)](operand)
        if isinstance(node, ast.BoolOp):
            values = [_eval_node(v, loc) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare):
            left = _eval_node(node.left, loc)
            for op, comp in zip(node.ops, node.comparators):
                right = _eval_node(comp, loc)
                cmp_map = {
                    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
                    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
                    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
                    ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
                    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
                }
                if not cmp_map[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            test = _eval_node(node.test, loc)
            return _eval_node(node.body if test else node.orelse, loc)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                elif isinstance(v, ast.Str):
                    parts.append(v.s)
                elif isinstance(v, ast.FormattedValue):
                    val = _eval_node(v.value, loc)
                    parts.append(str(val))
                else:
                    parts.append(str(_eval_node(v, loc)))
            return ''.join(parts)
        if isinstance(node, ast.ListComp):
            result = []
            _eval_comprehension(node, loc, safe_builtins, _eval_node, result.append)
            return result
        if isinstance(node, ast.DictComp):
            result = {}
            _eval_comprehension(node, loc, safe_builtins, _eval_node, lambda kv: result.update({kv[0]: kv[1]}))
            return result
        if isinstance(node, ast.SetComp):
            result = set()
            _eval_comprehension(node, loc, safe_builtins, _eval_node, result.add)
            return result
        raise ValueError(f"Unsupported AST node: {type(node).__name__}")

    def _eval_comprehension(comp, loc, builtins, eval_node_fn, collector):
        """Evaluate a comprehension (list/dict/set)."""
        def _step(gen_idx, current_loc):
            if gen_idx >= len(comp.generators):
                if isinstance(comp, ast.DictComp):
                    collector((eval_node_fn(comp.key, current_loc), eval_node_fn(comp.value, current_loc)))
                else:
                    collector(eval_node_fn(comp.elt, current_loc))
                return
            gen = comp.generators[gen_idx]
            iterable = eval_node_fn(gen.iter, current_loc)
            for item in iterable:
                new_loc = dict(current_loc)
                if isinstance(gen.target, ast.Name):
                    new_loc[gen.target.id] = item
                elif isinstance(gen.target, ast.Tuple):
                    for i, elt in enumerate(gen.target.elts):
                        if isinstance(elt, ast.Name):
                            new_loc[elt.id] = item[i]
                        else:
                            new_loc['_'] = item
                else:
                    new_loc['_'] = item
                ok = True
                for if_clause in gen.ifs:
                    if not eval_node_fn(if_clause, new_loc):
                        ok = False
                        break
                if ok:
                    _step(gen_idx + 1, new_loc)
        _step(0, dict(loc))

    return _eval_node(tree.body, local_vars)


def _clean_azure_base_url(provider_type: str, base_url: str) -> str:
    if provider_type != "azure" or not base_url:
        return base_url
    url = base_url.rstrip("/")
    if "/openai/" in url:
        url = url.split("/openai/")[0]
    if "?" in url:
        url = url.split("?")[0]
    if "cognitiveservices.azure.com" in url:
        url = url.replace("cognitiveservices.azure.com", "openai.azure.com")
    return url


async def execute_workflow(workflow_id: str, initial_data: dict = None, user_email: str = None) -> dict:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,))
        row = await cursor.fetchone()
        if not row:
            return {"error": "Workflow not found"}

        workflow = dict(row)
        nodes = json.loads(workflow.get("nodes", "[]"))
        edges = json.loads(workflow.get("edges", "[]"))

        # Create execution record
        exec_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT INTO workflow_executions (id, workflow_id, user_email, status, started_at, node_results)
               VALUES (?, ?, ?, 'running', ?, '{}')""",
            (exec_id, workflow_id, user_email, now)
        )
        await db.execute(
            "UPDATE workflows SET last_run_at=?, execution_count=execution_count+1, last_run_status='running', updated_at=? WHERE id=?",
            (now, now, workflow_id)
        )
        await db.commit()
    finally:
        await db.close()

    # Build adjacency map
    adj = {}
    for edge in edges:
        src = edge.get("source")
        tgt = edge.get("target")
        if src not in adj:
            adj[src] = []
        adj[src].append(tgt)

    # Find start nodes (no incoming edges)
    targets = {e.get("target") for e in edges}
    start_nodes = [n["id"] for n in nodes if n["id"] not in targets]
    if not start_nodes:
        start_nodes = [nodes[0]["id"]] if nodes else []

    # Node map
    node_map = {n["id"]: n for n in nodes}
    node_results = {}
    execution_order = []

    # BFS execution
    queue = list(start_nodes)
    visited = set()

    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        execution_order.append(node_id)

        node = node_map.get(node_id)
        if not node:
            continue

        node_type = node.get("type", "")
        node_data = node.get("data", {})
        start_time = time.time()

        # Gather input from previous nodes (deep copy to avoid circular refs)
        input_data = dict(initial_data) if initial_data else {}
        for edge in edges:
            if edge.get("target") == node_id:
                src_id = edge.get("source")
                if src_id in node_results:
                    prev_output = node_results[src_id].get("output", {})
                    if isinstance(prev_output, dict):
                        input_data.update({k: v for k, v in prev_output.items()})
                    else:
                        input_data["input"] = str(prev_output)

        try:
            result = await _execute_node(node_type, node_data, input_data)
            elapsed = int((time.time() - start_time) * 1000)
            node_results[node_id] = {
                "status": "success",
                "output": result,
                "execution_time_ms": elapsed,
                "node_name": node_data.get("label", node_id)
            }
        except Exception as e:
            elapsed = int((time.time() - start_time) * 1000)
            node_results[node_id] = {
                "status": "error",
                "error": str(e),
                "execution_time_ms": elapsed,
                "node_name": node_data.get("label", node_id)
            }

        # Check conditional branching
        if node_type == "conditional":
            condition_met = node_results[node_id].get("output", {}).get("condition_met", True)
            for edge in edges:
                if edge.get("source") == node_id:
                    handle = edge.get("sourceHandle", "")
                    if (condition_met and handle != "false") or (not condition_met and handle != "true"):
                        if edge.get("target") not in visited:
                            queue.append(edge["target"])
        else:
            # Add downstream nodes
            for next_id in adj.get(node_id, []):
                if next_id not in visited:
                    queue.append(next_id)

    # Update execution record
    db = await get_db()
    try:
        has_errors = any(r.get("status") == "error" for r in node_results.values())
        final_status = "failed" if has_errors else "completed"

        try:
            results_json = json.dumps(node_results, default=str)
        except (ValueError, TypeError):
            results_json = json.dumps({k: {"status": v.get("status","unknown"), "summary": str(v.get("output",""))[:2000]} for k,v in node_results.items()})
        await db.execute(
            """UPDATE workflow_executions SET status=?, completed_at=?, node_results=?
               WHERE id=?""",
            (final_status, datetime.utcnow().isoformat(), results_json, exec_id)
        )
        await db.execute(
            "UPDATE workflows SET last_run_status=?, updated_at=? WHERE id=?",
            (final_status, datetime.utcnow().isoformat(), workflow_id)
        )
        await db.commit()
    finally:
        await db.close()

    return {
        "execution_id": exec_id,
        "status": final_status,
        "node_results": node_results,
        "execution_order": execution_order,
    }


async def _execute_node(node_type: str, node_data: dict, input_data: dict) -> dict:
    if node_type == "trigger":
        return {"triggered": True, "data": input_data}

    elif node_type == "ai_completion":
        provider_id = node_data.get("provider_id")
        model_id = node_data.get("model_id")
        prompt = node_data.get("prompt", "")
        system_prompt = node_data.get("system_prompt", "")

        # Replace template variables
        for key, value in input_data.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))

        if not provider_id or not model_id:
            return {"error": "Provider and model required for AI completion"}

        db = await get_db()
        try:
            cursor = await db.execute("SELECT * FROM providers WHERE id = ?", (provider_id,))
            provider = await cursor.fetchone()
            if not provider:
                return {"error": "Provider not found"}
            provider = dict(provider)
        finally:
            await db.close()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        result = await non_stream_chat_completion(
            provider_type=provider["type"],
            model_id=model_id,
            messages=messages,
            api_key=provider["api_key_encrypted"],
            base_url=_clean_azure_base_url(provider["type"], provider.get("base_url")),
            api_version=provider.get("api_version"),
            provider_id=provider["id"],
            provider_name=provider["name"],
            source="workflow",
        )
        return {"content": result["content"], "usage": result["usage"], "latency_ms": result["latency_ms"]}

    elif node_type == "tool_exec":
        tool_name = node_data.get("tool_name", "")
        params = node_data.get("parameters", {})
        # Merge input data as potential parameters
        merged_params = {**params, **input_data}
        result = await execute_tool(tool_name, merged_params)
        return result

    elif node_type == "mcp_call":
        server_id = node_data.get("server_id", "")
        tool_name = node_data.get("tool_name", "")
        params = node_data.get("parameters", {})
        merged_params = {**params, **input_data}
        result = await execute_mcp_tool(server_id, tool_name, merged_params)
        return result

    elif node_type == "conditional":
        expression = node_data.get("expression", "true")
        try:
            safe_builtins = {
                "len": len, "str": str, "int": int, "float": float,
                "bool": bool, "list": list, "dict": dict,
                "True": True, "False": False, "None": None
            }
            # AST-walking expression evaluator — no exec/eval
            condition_met = bool(_safe_eval_expression(expression, safe_builtins, {"data": input_data}))
        except Exception as e:
            logger.debug("Conditional expression evaluation failed: %s", e)
            condition_met = True
        return {"condition_met": condition_met, "data": input_data}

    elif node_type == "transform":
        expression = node_data.get("expression", "data")
        try:
            safe_builtins = {
                "len": len, "str": str, "int": int, "float": float,
                "list": list, "dict": dict, "bool": bool,
                "True": True, "False": False, "None": None,
                "json": __import__('json'),
            }
            # AST-walking expression evaluator — no exec/eval
            result = _safe_eval_expression(expression, safe_builtins, {"data": input_data})
            return {"result": result}
        except Exception as e:
            return {"error": str(e)}

    elif node_type == "output":
        return {"output": input_data}

    elif node_type == "loop":
        items = input_data.get(node_data.get("items_key", "items"), [])
        results = []
        for item in items[:100]:  # limit iterations
            results.append(item)
        return {"items": results, "count": len(results)}

    elif node_type == "agent":
        agent_id = node_data.get("agent_id", "")
        prompt = node_data.get("prompt_template", "{{input}}")
        # Replace template variables
        for key, value in input_data.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))
        if not agent_id:
            return {"error": "agent_id required for agent node"}
        result = await run_agent(agent_id, prompt)
        return {"content": result.get("content", ""), "steps": result.get("steps", []), "iterations": result.get("iterations", 0)}

    else:
        return {"node_type": node_type, "status": "unsupported"}
