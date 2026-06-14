import json
import uuid
import httpx
import subprocess
import sys
import ast
import traceback
import logging
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)

BUILTIN_TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web using DuckDuckGo and return relevant results",
        "category": "search",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Maximum number of results", "default": 5}
            },
            "required": ["query"]
        }
    },
    {
        "name": "http_request",
        "description": "Make an HTTP request to any URL and return the response",
        "category": "network",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to request"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "default": "GET"},
                "headers": {"type": "object", "description": "Request headers", "default": {}},
                "body": {"type": "string", "description": "Request body for POST/PUT/PATCH"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "code_execute",
        "description": "Execute Python code in a sandboxed environment and return the output",
        "category": "compute",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Execution timeout in seconds", "default": 30}
            },
            "required": ["code"]
        }
    },
    {
        "name": "json_transform",
        "description": "Transform JSON data using a JMESPath-like expression or Python lambda",
        "category": "data",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "JSON string to transform"},
                "expression": {"type": "string", "description": "Python expression for transformation (use 'data' as variable)"}
            },
            "required": ["data", "expression"]
        }
    },
    {
        "name": "text_extract",
        "description": "Extract and parse text from a URL (HTML to plain text)",
        "category": "data",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to extract text from"},
                "selector": {"type": "string", "description": "Optional CSS selector to target specific content"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "calculator",
        "description": "Evaluate mathematical expressions safely. Supports arithmetic, trig, log, and more",
        "category": "math",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Math expression to evaluate (e.g. '2**10 + sqrt(144)')"}
            },
            "required": ["expression"]
        }
    },
    {
        "name": "date_time",
        "description": "Get the current date/time, convert timezones, or calculate date differences",
        "category": "utility",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["now", "convert", "diff"], "description": "Action to perform"},
                "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York')", "default": "UTC"},
                "date1": {"type": "string", "description": "First date (ISO 8601) for diff"},
                "date2": {"type": "string", "description": "Second date (ISO 8601) for diff"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_read",
        "description": "Read the contents of a local file",
        "category": "filesystem",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
                "encoding": {"type": "string", "description": "File encoding", "default": "utf-8"},
                "max_bytes": {"type": "integer", "description": "Max bytes to read", "default": 100000}
            },
            "required": ["path"]
        }
    },
    {
        "name": "file_write",
        "description": "Write content to a local file (creates or overwrites)",
        "category": "filesystem",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
                "mode": {"type": "string", "enum": ["write", "append"], "default": "write"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "regex_match",
        "description": "Apply a regex pattern to text and return all matches with groups",
        "category": "text",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to search"},
                "pattern": {"type": "string", "description": "Regex pattern"},
                "flags": {"type": "string", "description": "Regex flags (e.g. 'i' for case-insensitive)", "default": ""}
            },
            "required": ["text", "pattern"]
        }
    },
    {
        "name": "shell_execute",
        "description": "Execute a shell command and return stdout/stderr. Use with caution.",
        "category": "compute",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
                "cwd": {"type": "string", "description": "Working directory", "default": "."}
            },
            "required": ["command"]
        }
    },
    {
        "name": "text_summarize",
        "description": "Summarize a block of text by extracting the most important sentences",
        "category": "text",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to summarize"},
                "max_sentences": {"type": "integer", "description": "Max sentences in summary", "default": 5}
            },
            "required": ["text"]
        }
    },
    {
        "name": "csv_parse",
        "description": "Parse CSV text into structured JSON records",
        "category": "data",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "csv_text": {"type": "string", "description": "CSV content as a string"},
                "delimiter": {"type": "string", "description": "Delimiter character", "default": ","},
                "max_rows": {"type": "integer", "description": "Max rows to return", "default": 100}
            },
            "required": ["csv_text"]
        }
    },
    {
        "name": "base64_encode_decode",
        "description": "Encode text to base64 or decode base64 to text",
        "category": "utility",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["encode", "decode"], "description": "Encode or decode"},
                "text": {"type": "string", "description": "Text to encode or base64 string to decode"}
            },
            "required": ["action", "text"]
        }
    },
    {
        "name": "hash_generate",
        "description": "Generate a hash (MD5, SHA-256, SHA-512) of the given text",
        "category": "utility",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to hash"},
                "algorithm": {"type": "string", "enum": ["md5", "sha256", "sha512"], "default": "sha256"}
            },
            "required": ["text"]
        }
    }
]


async def seed_builtin_tools():
    db = await get_db()
    try:
        for tool in BUILTIN_TOOLS:
            cursor = await db.execute("SELECT id FROM tools WHERE name = ? AND type = 'builtin'", (tool["name"],))
            existing = await cursor.fetchone()
            if not existing:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    """INSERT INTO tools (id, name, description, type, category, parameters_schema, is_enabled, created_at, updated_at)
                       VALUES (?, ?, ?, 'builtin', ?, ?, 1, ?, ?)""",
                    (str(uuid.uuid4()), tool["name"], tool["description"], tool["category"],
                     json.dumps(tool["parameters_schema"]), now, now)
                )
        await db.commit()
    finally:
        await db.close()


async def execute_tool(tool_name: str, parameters: dict, tool_code: str = None, context: dict = None) -> dict:
    """
    Execute a tool by name with given parameters.

    Args:
        tool_name: Name of the tool to execute
        parameters: Parameters to pass to the tool
        tool_code: For custom tools, the Python code to execute
        context: Scope context for secrets access (workspace_id, environment_id, org_id)
    """
    try:
        if tool_name == "web_search":
            return await _exec_web_search(parameters)
        elif tool_name == "http_request":
            return await _exec_http_request(parameters)
        elif tool_name == "code_execute":
            return await _exec_code(parameters)
        elif tool_name == "json_transform":
            return await _exec_json_transform(parameters)
        elif tool_name == "text_extract":
            return await _exec_text_extract(parameters)
        elif tool_name == "calculator":
            return await _exec_calculator(parameters)
        elif tool_name == "date_time":
            return await _exec_date_time(parameters)
        elif tool_name == "file_read":
            return await _exec_file_read(parameters)
        elif tool_name == "file_write":
            return await _exec_file_write(parameters)
        elif tool_name == "regex_match":
            return await _exec_regex_match(parameters)
        elif tool_name == "shell_execute":
            return await _exec_shell(parameters)
        elif tool_name == "text_summarize":
            return await _exec_text_summarize(parameters)
        elif tool_name == "csv_parse":
            return await _exec_csv_parse(parameters)
        elif tool_name == "base64_encode_decode":
            return await _exec_base64(parameters)
        elif tool_name == "hash_generate":
            return await _exec_hash(parameters)
        elif tool_code:
            return await _exec_custom_tool(tool_code, parameters, context)
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


async def _exec_web_search(params: dict) -> dict:
    try:
        from duckduckgo_search import DDGS
        query = params.get("query", "")
        max_results = params.get("max_results", 5)
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return {"results": results, "query": query, "count": len(results)}
    except Exception as e:
        return {"error": f"Web search failed: {str(e)}"}


async def _exec_http_request(params: dict) -> dict:
    url = params.get("url", "")
    method = params.get("method", "GET").upper()
    headers = params.get("headers", {})
    body = params.get("body")

    async with httpx.AsyncClient(timeout=30.0) as client:
        kwargs = {"headers": headers}
        if body and method in ("POST", "PUT", "PATCH"):
            kwargs["content"] = body
        resp = await getattr(client, method.lower())(url, **kwargs)
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text[:10000],
            "url": str(resp.url)
        }


async def _exec_code(params: dict) -> dict:
    code = params.get("code", "")
    timeout = params.get("timeout", 30)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
            env={"PATH": "/usr/bin:/usr/local/bin"}
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"error": "Code execution timed out"}


async def _exec_json_transform(params: dict) -> dict:
    data_str = params.get("data", "{}")
    expression = params.get("expression", "data")
    try:
        data = json.loads(data_str)
        # Validate expression AST — only allow safe node types
        _SAFE_AST_NODES = {
            ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
            ast.BoolOp, ast.Compare, ast.Call, ast.Attribute, ast.Subscript,
            ast.List, ast.Tuple, ast.Dict, ast.Set, ast.ListComp, ast.DictComp,
            ast.SetComp, ast.comprehension, ast.Slice, ast.Index,
            ast.IfExp, ast.Num, ast.Str, ast.Bytes, ast.NameConstant,
            ast.JoinedStr, ast.FormattedValue, ast.keyword, ast.arg,
            ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
            ast.MatMult, ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd,
            ast.And, ast.Or, ast.Not, ast.Invert, ast.UAdd, ast.USub,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
            ast.In, ast.NotIn
        }
        tree = ast.parse(expression, mode='eval')
        for node in ast.walk(tree):
            if type(node) not in _SAFE_AST_NODES:
                return {"error": "Unsafe expression: forbidden construct"}
        restricted_builtins = {
            "len": len, "str": str, "int": int, "float": float,
            "list": list, "dict": dict, "sorted": sorted, "filter": filter,
            "map": map, "sum": sum, "min": min, "max": max,
            "enumerate": enumerate, "zip": zip, "range": range, "type": type,
            "isinstance": isinstance, "bool": bool,
            "True": True, "False": False, "None": None
        }
        result = eval(expression, {"__builtins__": restricted_builtins}, {"data": data})
        return {"result": result}
    except SyntaxError as e:
        return {"error": f"Invalid expression syntax: {str(e)}"}
    except Exception as e:
        return {"error": f"Transform failed: {str(e)}"}


async def _exec_text_extract(params: dict) -> dict:
    url = params.get("url", "")
    selector = params.get("selector")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    if selector:
        elements = soup.select(selector)
        text = "\n".join(el.get_text(strip=True) for el in elements)
    else:
        text = soup.get_text(separator="\n", strip=True)
    return {"text": text[:20000], "url": url, "length": len(text)}


async def _exec_calculator(params: dict) -> dict:
    import math
    import ast
    import operator
    expression = params.get("expression", "")
    safe_dict = {
        "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "log2": math.log2, "pi": math.pi, "e": math.e,
        "ceil": math.ceil, "floor": math.floor, "factorial": math.factorial,
    }
    _SAFE_OPERATORS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }
    def _safe_eval(node):
        if isinstance(node, ast.Expression):
            return _safe_eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            op = _SAFE_OPERATORS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            return op(_safe_eval(node.left), _safe_eval(node.right))
        if isinstance(node, ast.UnaryOp):
            op = _SAFE_OPERATORS.get(type(node.op))
            if op is None:
                raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
            return op(_safe_eval(node.operand))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in safe_dict:
                args = [_safe_eval(a) for a in node.args]
                return safe_dict[node.func.id](*args)
            raise ValueError(f"Unsupported function call: {ast.dump(node.func)}")
        if isinstance(node, ast.Name):
            if node.id in safe_dict:
                return safe_dict[node.id]
            raise ValueError(f"Unknown name: {node.id}")
        raise ValueError(f"Unsupported expression node: {ast.dump(node)}")
    try:
        parsed = ast.parse(expression, mode='eval')
        result = _safe_eval(parsed)
        return {"result": result, "expression": expression}
    except Exception as e:
        return {"error": f"Calculation failed: {str(e)}"}


async def _exec_date_time(params: dict) -> dict:
    from datetime import datetime as dt, timezone, timedelta
    action = params.get("action", "now")
    tz_name = params.get("timezone", "UTC")
    try:
        if action == "now":
            now = dt.now(timezone.utc)
            return {"utc": now.isoformat(), "timezone": tz_name, "unix": int(now.timestamp())}
        elif action == "diff":
            d1 = dt.fromisoformat(params.get("date1", ""))
            d2 = dt.fromisoformat(params.get("date2", ""))
            diff = d2 - d1
            return {"days": diff.days, "seconds": diff.total_seconds(), "human": str(diff)}
        return {"error": f"Unknown action: {action}"}
    except Exception as e:
        return {"error": f"Date/time error: {str(e)}"}


async def _exec_file_read(params: dict) -> dict:
    path = params.get("path", "")
    encoding = params.get("encoding", "utf-8")
    max_bytes = params.get("max_bytes", 100000)
    try:
        import os
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}
        with open(path, "r", encoding=encoding) as f:
            content = f.read(max_bytes)
        return {"content": content, "path": path, "length": len(content)}
    except Exception as e:
        return {"error": f"Read failed: {str(e)}"}


async def _exec_file_write(params: dict) -> dict:
    path = params.get("path", "")
    content = params.get("content", "")
    mode = params.get("mode", "write")
    try:
        file_mode = "a" if mode == "append" else "w"
        with open(path, file_mode) as f:
            f.write(content)
        return {"success": True, "path": path, "bytes_written": len(content)}
    except Exception as e:
        return {"error": f"Write failed: {str(e)}"}


async def _exec_regex_match(params: dict) -> dict:
    import re
    text = params.get("text", "")
    pattern = params.get("pattern", "")
    flags_str = params.get("flags", "")
    flags = 0
    if "i" in flags_str: flags |= re.IGNORECASE
    if "m" in flags_str: flags |= re.MULTILINE
    if "s" in flags_str: flags |= re.DOTALL
    try:
        matches = [{"match": m.group(), "groups": m.groups(), "start": m.start(), "end": m.end()}
                   for m in re.finditer(pattern, text, flags)]
        return {"matches": matches[:50], "count": len(matches), "pattern": pattern}
    except Exception as e:
        return {"error": f"Regex error: {str(e)}"}


async def _exec_shell(params: dict) -> dict:
    command = params.get("command", "")
    timeout = params.get("timeout", 30)
    cwd = params.get("cwd", ".")
    try:
        import shlex
        result = subprocess.run(
            shlex.split(command), shell=False, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
        return {"stdout": result.stdout[:10000], "stderr": result.stderr[:5000], "return_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out"}


async def _exec_text_summarize(params: dict) -> dict:
    text = params.get("text", "")
    max_sentences = params.get("max_sentences", 5)
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(sentences) <= max_sentences:
        return {"summary": text, "sentence_count": len(sentences)}
    scored = [(s, len(s.split())) for s in sentences]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [s[0] for s in scored[:max_sentences]]
    ordered = [s for s in sentences if s in top]
    return {"summary": " ".join(ordered), "original_sentences": len(sentences), "summary_sentences": len(ordered)}


async def _exec_csv_parse(params: dict) -> dict:
    import csv, io
    csv_text = params.get("csv_text", "")
    delimiter = params.get("delimiter", ",")
    max_rows = params.get("max_rows", 100)
    try:
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
        rows = []
        for i, row in enumerate(reader):
            if i >= max_rows: break
            rows.append(dict(row))
        return {"records": rows, "columns": list(rows[0].keys()) if rows else [], "count": len(rows)}
    except Exception as e:
        return {"error": f"CSV parse error: {str(e)}"}


async def _exec_base64(params: dict) -> dict:
    import base64 as b64
    action = params.get("action", "encode")
    text = params.get("text", "")
    try:
        if action == "encode":
            return {"result": b64.b64encode(text.encode()).decode()}
        else:
            return {"result": b64.b64decode(text.encode()).decode()}
    except Exception as e:
        return {"error": f"Base64 error: {str(e)}"}


async def _exec_hash(params: dict) -> dict:
    import hashlib
    text = params.get("text", "")
    algo = params.get("algorithm", "sha256")
    try:
        h = hashlib.new(algo)
        h.update(text.encode())
        return {"hash": h.hexdigest(), "algorithm": algo}
    except Exception as e:
        return {"error": f"Hash error: {str(e)}"}


async def _exec_custom_tool(code: str, parameters: dict, context: dict = None) -> dict:
    """
    Execute custom tool code with built-in get_secret() function.

    context should contain:
        - workspace_id: Current workspace ID (optional)
        - environment_id: Current environment ID (optional)
        - org_id: Current organization ID (optional)
    """
    import os as _os
    context = context or {}
    # Use defaults if context values are empty
    workspace_id = context.get("workspace_id") or "default-workspace"
    environment_id = context.get("environment_id") or "default-env"
    org_id = context.get("org_id") or "default-org"

    # Validate IDs to prevent injection via context
    import re
    _ID_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')
    if not _ID_RE.match(workspace_id) or not _ID_RE.match(environment_id) or not _ID_RE.match(org_id):
        return {"error": "Invalid context identifiers"}

    # Path to the database file
    db_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "agentic_platform.db")

    # Build the get_secret helper code that will be injected into the subprocess
    get_secret_code = f'''
def get_secret(secret_name):
    """
    Get a secret value by name. Searches in order:
    1. Workspace scope (highest priority)
    2. Environment scope
    3. Organization scope (lowest priority)
    Raises ValueError if secret not found or no access.
    """
    import sqlite3

    db_path = {repr(db_path)}
    workspace_id = {repr(workspace_id)}
    environment_id = {repr(environment_id)}
    org_id = {repr(org_id)}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # If we have a workspace_id, resolve its env_id and org_id for inheritance
    if workspace_id:
        cursor.execute(
            "SELECT org_id, env_id FROM workspaces WHERE id = ?",
            (workspace_id,)
        )
        ws_row = cursor.fetchone()
        if ws_row:
            org_id = ws_row[0] or org_id
            environment_id = ws_row[1] or environment_id

    # Try scopes in priority order: workspace -> environment -> organization
    scopes_to_try = []
    if workspace_id:
        scopes_to_try.append(("workspace", workspace_id))
    if environment_id:
        scopes_to_try.append(("env", environment_id))
    if org_id:
        scopes_to_try.append(("org", org_id))

    for scope_type, scope_id in scopes_to_try:
        cursor.execute(
            "SELECT value_encrypted FROM secrets WHERE name = ? AND scope_type = ? AND scope_id = ?",
            (secret_name, scope_type, scope_id)
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return row[0]

    conn.close()

    # Build helpful error message about which scopes were checked
    checked_scopes = []
    if workspace_id:
        checked_scopes.append(f"workspace '{{workspace_id}}'")
    if environment_id:
        checked_scopes.append(f"environment '{{environment_id}}'")
    if org_id:
        checked_scopes.append(f"organization '{{org_id}}'")

    if checked_scopes:
        raise ValueError(f"Secret '{{secret_name}}' not found in: {{', '.join(checked_scopes)}}")
    else:
        raise ValueError(f"Secret '{{secret_name}}' not found - no scope context available")
'''

    full_code = f"""
import json, sys
{get_secret_code}
params = json.loads('''{json.dumps(parameters)}''')
{code}
if 'result' in dir():
    print(json.dumps({{"result": result}}))
elif 'output' in dir():
    print(json.dumps({{"result": output}}))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", full_code],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        return {"stdout": result.stdout, "stderr": result.stderr, "return_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "Custom tool execution timed out"}
    except Exception as e:
        return {"error": str(e)}


def get_tool_schema_for_llm(tool: dict) -> dict:
    params = tool.get("parameters_schema", {})
    if isinstance(params, str):
        params = json.loads(params)
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": params
        }
    }
