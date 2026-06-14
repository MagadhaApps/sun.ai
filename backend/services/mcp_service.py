import json
import uuid
import subprocess  # Used for MCP server lifecycle; all calls validated & run with shell=False
import os
import signal
import asyncio
import threading
import logging
import re
from datetime import datetime
from database import get_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP JSON-RPC stdio protocol helpers
# ---------------------------------------------------------------------------

def _encode_mcp_message(msg: dict) -> bytes:
    """Encode a dict as an MCP JSON-RPC message with Content-Length header."""
    data = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(data)}\r\n\r\n".encode("utf-8")
    return header + data


def _encode_mcp_ndjson(msg: dict) -> bytes:
    """Encode a dict as a newline-delimited JSON message (for FastMCP)."""
    return (json.dumps(msg) + "\n").encode("utf-8")


MCP_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "sunny-ai", "version": "1.0.0"},
    },
}

MCP_INITIALIZED_NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}

BUILTIN_MCP_SERVERS = [
    {
        "name": "github",
        "description": "GitHub API integration - manage repos, issues, PRs, branches, and code search",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")},
        "available_tools": [
            {"name": "list_repos", "description": "List repositories for a user or organization", "parameters": {"owner": "string"}},
            {"name": "list_issues", "description": "List issues in a repository", "parameters": {"repo": "string", "state": "string"}},
            {"name": "create_issue", "description": "Create a new GitHub issue", "parameters": {"repo": "string", "title": "string", "body": "string"}},
            {"name": "create_pull_request", "description": "Create a pull request", "parameters": {"repo": "string", "title": "string", "head": "string", "base": "string"}},
            {"name": "search_code", "description": "Search code across GitHub", "parameters": {"query": "string"}},
            {"name": "get_file_contents", "description": "Get file contents from a repository", "parameters": {"repo": "string", "path": "string"}}
        ]
    },
    {
        "name": "docker",
        "description": "Docker container management - list, create, start, stop containers and images",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-docker"],
        "available_tools": [
            {"name": "list_containers", "description": "List Docker containers (running and stopped)", "parameters": {"all": "boolean"}},
            {"name": "list_images", "description": "List available Docker images", "parameters": {}},
            {"name": "create_container", "description": "Create a new container from an image", "parameters": {"image": "string", "name": "string"}},
            {"name": "start_container", "description": "Start a stopped container", "parameters": {"container_id": "string"}},
            {"name": "stop_container", "description": "Stop a running container", "parameters": {"container_id": "string"}},
            {"name": "remove_container", "description": "Remove a container", "parameters": {"container_id": "string", "force": "boolean"}},
            {"name": "container_logs", "description": "Get logs from a container", "parameters": {"container_id": "string", "tail": "number"}},
            {"name": "run_container", "description": "Run a command in a new container and return output", "parameters": {"image": "string", "command": "string"}}
        ]
    }
]

_running_processes = {}
_output_readers = {}  # Store output reader threads to keep them alive
_mcp_tool_processes = {}  # Store long-lived MCP subprocesses for execute_mcp_tool


def _drain_pipe(pipe, server_id: str, pipe_name: str):
    """Background thread to drain pipe output and prevent buffer overflow."""
    try:
        while True:
            line = pipe.readline()
            if not line:
                break
            # Optionally log output for debugging
            # print(f"[MCP {server_id}] {pipe_name}: {line.decode('utf-8', errors='replace').strip()}")
    except Exception as e:
        logger.debug("Drain pipe %s for server %s: %s", pipe_name, server_id, e)
    finally:
        try:
            pipe.close()
        except Exception as e:
            logger.debug("Pipe %s close for server %s: %s", pipe_name, server_id, e)


async def seed_builtin_mcp_servers():
    db = await get_db()
    try:
        for server in BUILTIN_MCP_SERVERS:
            cursor = await db.execute("SELECT id, env FROM mcp_servers WHERE name = ? AND type = 'builtin'", (server["name"],))
            existing = await cursor.fetchone()
            if not existing:
                now = datetime.utcnow().isoformat()
                await db.execute(
                    """INSERT INTO mcp_servers (id, name, type, command, args, env, status, description, available_tools, created_at, updated_at)
                       VALUES (?, ?, 'builtin', ?, ?, ?, 'stopped', ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), server["name"], server["command"],
                     json.dumps(server["args"]), json.dumps(server.get("env", {})),
                     server["description"],
                     json.dumps(server["available_tools"]), now, now)
                )
            elif server.get("env"):
                # Backfill env placeholders for existing servers that have none
                row = dict(existing)
                current_env = row.get("env") or "{}"
                if current_env in ("{}", "", None):
                    await db.execute(
                        "UPDATE mcp_servers SET env = ? WHERE id = ?",
                        (json.dumps(server["env"]), row["id"])
                    )
        await db.commit()
    finally:
        await db.close()


async def _resolve_server_env(server: dict, db, org_id: str = None) -> tuple:
    """Resolve MCP server env vars from org-level secrets.

    Checks three sources in priority order:
      1. Org-level secrets (from the secrets table)
      2. Non-empty value already stored in the server env field
      3. System environment variable

    If *org_id* is provided it is used directly; otherwise the org is
    derived from the server's workspace.

    Returns (resolved_env_dict, missing_keys_list).
    ``missing_keys_list`` items are dicts: {"key": str, "description": str}.
    """
    raw_env = server.get("env", "{}")
    env_template = json.loads(raw_env) if isinstance(raw_env, str) else (raw_env or {})

    if not env_template:
        return {}, []

    # Use the caller-supplied org_id, or derive from the server's workspace
    if not org_id:
        org_id = "default-org"
        workspace_id = server.get("workspace_id")
        if workspace_id:
            cursor = await db.execute(
                "SELECT org_id FROM workspaces WHERE id = ?", (workspace_id,)
            )
            ws_row = await cursor.fetchone()
            if ws_row and ws_row["org_id"]:
                org_id = ws_row["org_id"]

    resolved = {}
    missing = []

    for key, template_val in env_template.items():
        # 1) Org-level secret
        cursor = await db.execute(
            "SELECT value_encrypted FROM secrets WHERE scope_type = 'org' AND scope_id = ? AND name = ?",
            (org_id, key),
        )
        secret_row = await cursor.fetchone()

        if secret_row and secret_row["value_encrypted"]:
            resolved[key] = secret_row["value_encrypted"]
        elif template_val:
            resolved[key] = template_val
        elif os.environ.get(key):
            resolved[key] = os.environ[key]
        else:
            missing.append({
                "key": key,
                "description": f"Required by {server.get('name', 'MCP server')}",
            })

    return resolved, missing


async def discover_mcp_tools(server_id: str, org_id: str = None) -> dict:
    """Discover available tools from an MCP server by spawning a temporary subprocess."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            return {"error": "Server not found"}

        server = dict(row)

        # Resolve env vars from org-level secrets
        resolved_env, missing = await _resolve_server_env(server, db, org_id=org_id)
        if missing:
            key_names = ", ".join(m["key"] for m in missing)
            return {"error": f"Missing required configuration: {key_names}"}

        command = server["command"]
        raw_args = server.get("args", "[]")
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or [])

        env = {**os.environ, **resolved_env}
        backend_dir = os.path.dirname(os.path.dirname(__file__))

        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                command, *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=backend_dir,
                env=env,
            )

            async def _write_ndjson(msg: dict):
                process.stdin.write(_encode_mcp_ndjson(msg))
                await process.stdin.drain()

            # Give server time to start
            await asyncio.sleep(1)

            # 1) Initialize (using NDJSON format for FastMCP compatibility)
            await _write_ndjson(MCP_INIT_REQUEST)
            init_resp = await asyncio.wait_for(_read_universal_mcp_response(process.stdout, 1), timeout=120)
            if init_resp is None:
                return {"error": f"MCP server '{server['name']}' did not respond to initialize"}

            # 2) Initialized notification
            await _write_ndjson(MCP_INITIALIZED_NOTIFICATION)

            # 3) Request tools/list
            await _write_ndjson({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            })
            tools_resp = await asyncio.wait_for(_read_universal_mcp_response(process.stdout, 2), timeout=60)

            if tools_resp and "result" in tools_resp:
                tools = tools_resp["result"].get("tools", [])
                # Convert to our format
                available_tools = []
                for t in tools:
                    tool_entry = {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": {}
                    }
                    # Extract parameters from inputSchema
                    input_schema = t.get("inputSchema", {})
                    props = input_schema.get("properties", {})
                    for param_name, param_info in props.items():
                        tool_entry["parameters"][param_name] = param_info.get("type", "string")
                    available_tools.append(tool_entry)

                # Update database with discovered tools
                await db.execute(
                    "UPDATE mcp_servers SET available_tools = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(available_tools), datetime.utcnow().isoformat(), server_id)
                )
                await db.commit()

                return {"tools": available_tools, "count": len(available_tools)}
            else:
                return {"error": "No tools response from MCP server", "response": tools_resp}

        except asyncio.TimeoutError:
            return {"error": f"MCP server '{server['name']}' timed out during tool discovery"}
        except Exception as e:
            return {"error": f"Tool discovery failed: {str(e)}"}
        finally:
            if process:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass  # Process already exited
                except Exception as e:
                    logger.debug("Cleanup error during shutdown of server %s: %s", server["name"], e)
    finally:
        await db.close()


async def start_mcp_server(server_id: str, org_id: str = None) -> dict:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            return {"error": "Server not found"}

        server = dict(row)

        if server_id in _running_processes:
            return {"status": "already_running", "pid": _running_processes[server_id].pid}

        # Resolve env vars from org-level secrets store
        resolved_env, missing = await _resolve_server_env(server, db, org_id=org_id)
        if missing:
            return {
                "status": "missing_env",
                "required_keys": missing,
                "server_name": server["name"],
            }

        command = server["command"]
        args = json.loads(server.get("args", "[]"))
        env = {**os.environ, **resolved_env}

        # Validate command and args to prevent injection
        if not command or not isinstance(command, str):
            return {"error": "Invalid command: must be a non-empty string"}
        # Only allow alphanumeric, dash, underscore, dot, slash, and space in command path
        if not re.match(r'^[a-zA-Z0-9_\-./\\ ]+$', command):
            return {"error": "Invalid command: contains disallowed characters"}
        for arg in args:
            if not isinstance(arg, str):
                return {"error": "Invalid args: all args must be strings"}

        backend_dir = os.path.dirname(os.path.dirname(__file__))
        full_args = [command] + args

        try:
            # Use start_new_session to prevent signal propagation from parent
            # Use subprocess.PIPE for stdin to keep it open - MCP servers use
            # stdio transport and will exit immediately if stdin gets EOF
            # (which subprocess.DEVNULL causes)
            process = subprocess.Popen(
                full_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=backend_dir,
                env=env,
                start_new_session=True,
                shell=False,
            )
            _running_processes[server_id] = process

            # Send MCP protocol initialization handshake.
            # MCP servers expect an initialize + initialized sequence before
            # they enter their main loop; without it some servers (e.g. Docker)
            # will time out and exit.
            # Use NDJSON format for FastMCP compatibility.
            try:
                process.stdin.write(_encode_mcp_ndjson(MCP_INIT_REQUEST))
                process.stdin.write(_encode_mcp_ndjson(MCP_INITIALIZED_NOTIFICATION))
                process.stdin.flush()
            except Exception as e:
                logger.debug("stdin write for server %s: %s", server_id, e)

            # Start background threads to drain stdout and stderr
            # This prevents buffer overflow which causes process hangs
            stdout_thread = threading.Thread(
                target=_drain_pipe,
                args=(process.stdout, server_id, "stdout"),
                daemon=True
            )
            stderr_thread = threading.Thread(
                target=_drain_pipe,
                args=(process.stderr, server_id, "stderr"),
                daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()
            _output_readers[server_id] = (stdout_thread, stderr_thread)

            await db.execute(
                "UPDATE mcp_servers SET status='running', pid=?, updated_at=? WHERE id=?",
                (process.pid, datetime.utcnow().isoformat(), server_id)
            )
            await db.commit()

            return {"status": "running", "pid": process.pid}
        except Exception as e:
            return {"error": f"Failed to start server: {str(e)}"}
    finally:
        await db.close()


async def stop_mcp_server(server_id: str) -> dict:
    if server_id in _running_processes:
        process = _running_processes[server_id]
        try:
            # Close stdin pipe first so the MCP server gets EOF and can
            # begin a graceful shutdown before we send SIGTERM
            try:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
            except Exception as e:
                logger.debug("stdin close for server %s: %s", server_id, e)
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    process.kill()
            del _running_processes[server_id]
        except Exception as e:
            logger.debug("Process cleanup for server %s: %s", server_id, e)

    # Clean up reader threads reference (they're daemon threads, so they'll die with process)
    if server_id in _output_readers:
        del _output_readers[server_id]

    db = await get_db()
    try:
        await db.execute(
            "UPDATE mcp_servers SET status='stopped', pid=NULL, updated_at=? WHERE id=?",
            (datetime.utcnow().isoformat(), server_id)
        )
        await db.commit()
        return {"status": "stopped"}
    finally:
        await db.close()


async def get_server_status(server_id: str) -> str:
    if server_id in _running_processes:
        process = _running_processes[server_id]
        if process.poll() is None:
            return "running"
        else:
            del _running_processes[server_id]
            # Clean up reader threads reference
            if server_id in _output_readers:
                del _output_readers[server_id]
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE mcp_servers SET status='stopped', pid=NULL, updated_at=? WHERE id=?",
                    (datetime.utcnow().isoformat(), server_id)
                )
                await db.commit()
            finally:
                await db.close()
            return "stopped"
    return "stopped"


async def execute_mcp_tool(server_id: str, tool_name: str, parameters: dict, org_id: str = None) -> dict:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            return {"error": "Server not found"}

        server = dict(row)
        server_name = server["name"]

        # Resolve env vars from org-level secrets first (needed for both direct and subprocess handlers)
        resolved_env, missing = await _resolve_server_env(server, db, org_id=org_id)

        # Fast path: direct Python implementations for a few common servers
        if server_name in _DIRECT_HANDLERS:
            # Check for missing required keys for servers that need them
            if missing and server_name in ("github", "docker", "brave_search", "slack", "notion"):
                key_names = ", ".join(m["key"] for m in missing)
                return {
                    "error": f"Missing required configuration: {key_names}. "
                             "Configure them in Secrets & Variables at the organization level."
                }
            try:
                # agent_service maps MCP tools with a `<server_name>__` prefix, strip it for direct handlers
                raw_tool_name = tool_name
                prefix = f"{server_name}__"
                if tool_name.startswith(prefix):
                    raw_tool_name = tool_name[len(prefix):]
                return await _DIRECT_HANDLERS[server_name](raw_tool_name, parameters, resolved_env)
            except Exception as e:
                return {"error": str(e)}
        if missing:
            key_names = ", ".join(m["key"] for m in missing)
            return {
                "error": f"Missing required configuration: {key_names}. "
                         "Configure them in Secrets & Variables at the organization level."
            }

        # For every other server (builtin or custom), execute the tool via the persistent MCP subprocess.
        return await _execute_via_mcp_subprocess(server_id, server, tool_name, parameters, resolved_env=resolved_env)
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Generic MCP subprocess execution (works for ANY MCP server)
# ---------------------------------------------------------------------------

async def _read_mcp_message(stdout) -> dict:
    """Read a single Content-Length-framed MCP JSON-RPC message."""
    header_buf = b""
    while not header_buf.endswith(b"\r\n\r\n"):
        ch = await stdout.read(1)
        if not ch:
            return None
        header_buf += ch
    length = None
    for line in header_buf.decode("utf-8", errors="replace").split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":")[1].strip())
            break
    if length is None:
        return None
    body = await stdout.readexactly(length)
    return json.loads(body.decode("utf-8"))


async def _read_universal_mcp_message(stdout) -> dict:
    """Read either an NDJSON message or a Content-Length JSON-RPC message."""
    while True:
        line = await stdout.readline()
        if not line:
            return None
        
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        
        if text.startswith("{"):
            # It's NDJSON
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        elif text.lower().startswith("content-length:"):
            # It's standard MCP with HTTP-like headers
            length = int(text.split(":")[1].strip())
            # Read until empty line
            while True:
                hdr_line = await stdout.readline()
                if not hdr_line or hdr_line == b"\r\n" or hdr_line == b"\n":
                    break
            
            body = await stdout.readexactly(length)
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return None

async def _read_universal_mcp_response(stdout, expected_id: int, limit: int = 100) -> dict:
    """Read messages universally until we find the JSON-RPC response with *expected_id*."""
    for _ in range(limit):
        msg = await _read_universal_mcp_message(stdout)
        if msg is None:
            continue
        if msg.get("id") == expected_id:
            return msg
    return None


async def _execute_via_mcp_subprocess(server_id: str, server: dict, tool_name: str, parameters: dict, resolved_env: dict = None) -> dict:
    """Execute a tool using a persistent MCP server subprocess. Spawns it if not running."""
    if server_id not in _mcp_tool_processes:
        _mcp_tool_processes[server_id] = {
            "lock": asyncio.Lock(),
            "process": None,
            "next_req_id": 2,
            "stderr_task": None
        }

    state = _mcp_tool_processes[server_id]
    
    async with state["lock"]:
        # Clean up dead process
        if state["process"] and state["process"].returncode is not None:
            state["process"] = None
            if state["stderr_task"]:
                state["stderr_task"].cancel()
                state["stderr_task"] = None

        # Spawn if necessary
        if state["process"] is None:
            command = server["command"]
            raw_args = server.get("args", "[]")
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or [])

            env = {**os.environ}
            if resolved_env:
                env.update(resolved_env)
            else:
                raw_env = server.get("env", "{}")
                env_vars = json.loads(raw_env) if isinstance(raw_env, str) else (raw_env or {})
                for k, v in env_vars.items():
                    if v:
                        env[k] = v

            backend_dir = os.path.dirname(os.path.dirname(__file__))

            try:
                process = await asyncio.create_subprocess_exec(
                    command, *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=backend_dir,
                    env=env,
                )
            except Exception as e:
                return {"error": f"Failed to spawn MCP server '{server.get('name', '?')}': {str(e)}"}

            state["process"] = process
            state["next_req_id"] = 2

            async def _stderr_drain(stderr):
                try:
                    while True:
                        line = await stderr.readline()
                        if not line:
                            break
                except asyncio.CancelledError:
                    logger.debug("Stderr drain task cancelled for server %s during shutdown", server_id)
                except Exception as e:
                    logger.debug("Non-critical error draining stderr for server %s: %s", server_id, e)

            state["stderr_task"] = asyncio.create_task(_stderr_drain(process.stderr))

            def _write_universal_sync(msg: dict):
                process.stdin.write(_encode_mcp_message(msg))

            try:
                # 1) initialize 
                _write_universal_sync(MCP_INIT_REQUEST)
                await process.stdin.drain()
                init_resp = await asyncio.wait_for(_read_universal_mcp_response(process.stdout, 1), timeout=120)
                if init_resp is None:
                    state["process"].kill()
                    state["process"] = None
                    return {"error": f"MCP server '{server.get('name', '?')}' failed to initialize."}

                # 2) initialized notification
                _write_universal_sync(MCP_INITIALIZED_NOTIFICATION)
                await process.stdin.drain()
            except asyncio.TimeoutError:
                state["process"].kill()
                state["process"] = None
                return {"error": f"MCP server '{server.get('name', '?')}' timed out during initialization."}
            except Exception as e:
                state["process"].kill()
                state["process"] = None
                return {"error": f"MCP server setup failed: {str(e)}"}
            
            # Brief delay to allow the server to fully stabilize before accepting tools
            await asyncio.sleep(1)

        # Process is healthy and ready to accept tool calls
        process = state["process"]
        req_id = state["next_req_id"]
        state["next_req_id"] += 1

        call_req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": parameters},
        }

        try:
            process.stdin.write(_encode_mcp_message(call_req))
            await process.stdin.drain()

            tool_resp = await asyncio.wait_for(_read_universal_mcp_response(process.stdout, req_id), timeout=300)

            if tool_resp and "result" in tool_resp:
                content = tool_resp["result"].get("content", [])
                parts = []
                for item in content:
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    else:
                        parts.append(json.dumps(item))
                return {"result": "\n".join(parts) if parts else json.dumps(tool_resp["result"])}
            elif tool_resp and "error" in tool_resp:
                return {"error": tool_resp["error"].get("message", "MCP tool execution failed")}
            else:
                return {"error": "No valid response from MCP server"}
        except asyncio.TimeoutError:
            return {"error": "Tool execution timed out."}
        except Exception as e:
            # If the pipe is broken or process dies mid-call, clear it to respawn next time
            state["process"] = None
            return {"error": f"MCP execution failed: {str(e)}"}


# Map of server names that have fast direct Python handlers
_DIRECT_HANDLERS = {
    "filesystem": lambda tool, params, env: _fs_tool(tool, params),
    "database": lambda tool, params, env: _db_tool(tool, params),
    "web_scraper": lambda tool, params, env: _scraper_tool(tool, params),
    "github": lambda tool, params, env: _github_tool(tool, params, env),
    "docker": lambda tool, params, env: _docker_tool(tool, params, env),
}


async def _fs_tool(tool_name: str, params: dict) -> dict:
    import aiofiles
    path = params.get("path", ".")
    if tool_name == "read_file":
        with open(path, "r") as f:
            content = f.read()
        return {"content": content[:50000], "path": path, "size": len(content)}
    elif tool_name == "write_file":
        content = params.get("content", "")
        with open(path, "w") as f:
            f.write(content)
        return {"written": True, "path": path, "size": len(content)}
    elif tool_name == "list_directory":
        entries = []
        for entry in os.scandir(path):
            entries.append({
                "name": entry.name,
                "type": "directory" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None
            })
        return {"entries": entries, "path": path, "count": len(entries)}
    elif tool_name == "search_files":
        pattern = params.get("pattern", "*")
        import glob
        matches = glob.glob(os.path.join(path, "**", pattern), recursive=True)
        return {"matches": matches[:100], "count": len(matches)}
    return {"error": f"Unknown filesystem tool: {tool_name}"}


async def _db_tool(tool_name: str, params: dict) -> dict:
    import aiosqlite
    db_path = params.get("db_path", "test.db")
    if tool_name == "execute_query":
        query = params.get("query", "")
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute(query)
            if query.strip().upper().startswith("SELECT"):
                rows = await cursor.fetchall()
                return {"rows": [dict(r) for r in rows], "count": len(rows)}
            else:
                await db.commit()
                return {"affected_rows": cursor.rowcount}
        finally:
            await db.close()
    elif tool_name == "list_tables":
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            rows = await cursor.fetchall()
            return {"tables": [dict(r)["name"] for r in rows]}
        finally:
            await db.close()
    elif tool_name == "describe_table":
        table = params.get("table_name", "")
        # Validate table name against safe pattern to prevent SQL injection
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table):
            return {"error": "Invalid table name"}
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            cursor = await db.execute(f"PRAGMA table_info({table})")
            rows = await cursor.fetchall()
            return {"columns": [dict(r) for r in rows]}
        finally:
            await db.close()
    return {"error": f"Unknown database tool: {tool_name}"}


async def _scraper_tool(tool_name: str, params: dict) -> dict:
    import httpx
    from bs4 import BeautifulSoup
    url = params.get("url", "")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    if tool_name == "fetch_page":
        return {"html": resp.text[:50000], "status": resp.status_code, "url": str(resp.url)}
    elif tool_name == "extract_text":
        selector = params.get("selector")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        if selector:
            elements = soup.select(selector)
            text = "\n".join(el.get_text(strip=True) for el in elements)
        else:
            text = soup.get_text(separator="\n", strip=True)
        return {"text": text[:20000], "url": url}
    elif tool_name == "extract_links":
        links = []
        for a in soup.find_all("a", href=True):
            links.append({"text": a.get_text(strip=True), "href": a["href"]})
        return {"links": links[:200], "count": len(links)}
    return {"error": f"Unknown scraper tool: {tool_name}"}


async def _github_tool(tool_name: str, params: dict, env: dict) -> dict:
    """Direct Python handler for GitHub API operations."""
    import httpx

    token = env.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        return {"error": "GITHUB_PERSONAL_ACCESS_TOKEN not configured. Add it in Secrets & Variables at the organization level."}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        try:
            if tool_name == "list_issues":
                repo = params.get("repo", "")
                state = params.get("state", "open")
                if not repo:
                    return {"error": "repo parameter is required (format: owner/repo)"}
                url = f"https://api.github.com/repos/{repo}/issues"
                resp = await client.get(url, params={"state": state, "per_page": 30})
                resp.raise_for_status()
                issues = resp.json()
                return {
                    "issues": [
                        {
                            "number": i["number"],
                            "title": i["title"],
                            "state": i["state"],
                            "user": i["user"]["login"],
                            "created_at": i["created_at"],
                            "labels": [l["name"] for l in i.get("labels", [])],
                            "url": i["html_url"],
                        }
                        for i in issues
                    ],
                    "count": len(issues),
                }

            elif tool_name == "create_issue":
                repo = params.get("repo", "")
                title = params.get("title", "")
                body = params.get("body", "")
                if not repo or not title:
                    return {"error": "repo and title parameters are required"}
                url = f"https://api.github.com/repos/{repo}/issues"
                resp = await client.post(url, json={"title": title, "body": body})
                resp.raise_for_status()
                issue = resp.json()
                return {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["html_url"],
                    "created": True,
                }

            elif tool_name == "get_file_contents":
                repo = params.get("repo", "")
                path = params.get("path", "")
                ref = params.get("ref", "")
                if not repo or not path:
                    return {"error": "repo and path parameters are required"}
                url = f"https://api.github.com/repos/{repo}/contents/{path}"
                query_params = {"ref": ref} if ref else {}
                resp = await client.get(url, params=query_params)
                resp.raise_for_status()
                data = resp.json()
                if data.get("type") == "file":
                    import base64
                    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                    return {
                        "path": data["path"],
                        "content": content[:50000],
                        "size": data["size"],
                        "sha": data["sha"],
                    }
                elif data.get("type") == "dir" or isinstance(data, list):
                    # It's a directory listing
                    entries = data if isinstance(data, list) else [data]
                    return {
                        "path": path,
                        "type": "directory",
                        "entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in entries],
                    }
                return {"error": f"Unknown content type: {data.get('type')}"}

            elif tool_name == "search_code":
                query = params.get("query", "")
                if not query:
                    return {"error": "query parameter is required"}
                url = "https://api.github.com/search/code"
                resp = await client.get(url, params={"q": query, "per_page": 20})
                resp.raise_for_status()
                data = resp.json()
                return {
                    "total_count": data["total_count"],
                    "items": [
                        {
                            "name": item["name"],
                            "path": item["path"],
                            "repo": item["repository"]["full_name"],
                            "url": item["html_url"],
                        }
                        for item in data.get("items", [])
                    ],
                }

            elif tool_name == "create_pull_request":
                repo = params.get("repo", "")
                title = params.get("title", "")
                head = params.get("head", "")
                base = params.get("base", "main")
                body = params.get("body", "")
                if not repo or not title or not head:
                    return {"error": "repo, title, and head parameters are required"}
                url = f"https://api.github.com/repos/{repo}/pulls"
                resp = await client.post(url, json={"title": title, "head": head, "base": base, "body": body})
                resp.raise_for_status()
                pr = resp.json()
                return {
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr["html_url"],
                    "created": True,
                }

            elif tool_name == "list_repos":
                # List repositories for the authenticated user or a specific user/org
                owner = params.get("owner", "")
                if owner:
                    url = f"https://api.github.com/users/{owner}/repos"
                else:
                    url = "https://api.github.com/user/repos"
                resp = await client.get(url, params={"per_page": 30, "sort": "updated"})
                resp.raise_for_status()
                repos = resp.json()
                return {
                    "repos": [
                        {
                            "name": r["name"],
                            "full_name": r["full_name"],
                            "description": r.get("description", ""),
                            "url": r["html_url"],
                            "stars": r["stargazers_count"],
                            "language": r.get("language"),
                        }
                        for r in repos
                    ],
                    "count": len(repos),
                }

            else:
                return {"error": f"Unknown GitHub tool: {tool_name}"}

        except httpx.HTTPStatusError as e:
            error_body = e.response.text[:500] if e.response else ""
            return {"error": f"GitHub API error ({e.response.status_code}): {error_body}"}
        except Exception as e:
            return {"error": f"GitHub API error: {str(e)}"}


async def _docker_tool(tool_name: str, params: dict, env: dict) -> dict:
    """Direct Python handler for Docker operations."""
    try:
        import docker
        client = docker.from_env()
    except Exception as e:
        return {"error": f"Failed to connect to Docker: {str(e)}. Ensure Docker is running."}

    try:
        if tool_name == "list_containers":
            all_containers = params.get("all", True)
            containers = client.containers.list(all=all_containers)
            return {
                "containers": [
                    {
                        "id": c.short_id,
                        "name": c.name,
                        "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                        "status": c.status,
                        "ports": c.ports,
                    }
                    for c in containers
                ],
                "count": len(containers),
            }

        elif tool_name == "list_images":
            images = client.images.list()
            return {
                "images": [
                    {
                        "id": img.short_id,
                        "tags": img.tags,
                        "size": img.attrs.get("Size", 0) // (1024 * 1024),  # MB
                        "created": img.attrs.get("Created", ""),
                    }
                    for img in images
                ],
                "count": len(images),
            }

        elif tool_name == "create_container":
            image = params.get("image", "")
            name = params.get("name", "")
            if not image:
                return {"error": "image parameter is required"}
            kwargs = {"image": image, "detach": True}
            if name:
                kwargs["name"] = name
            container = client.containers.create(**kwargs)
            return {
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "created": True,
            }

        elif tool_name == "start_container":
            container_id = params.get("container_id", "")
            if not container_id:
                return {"error": "container_id parameter is required"}
            container = client.containers.get(container_id)
            container.start()
            container.reload()
            return {
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "started": True,
            }

        elif tool_name == "stop_container":
            container_id = params.get("container_id", "")
            if not container_id:
                return {"error": "container_id parameter is required"}
            container = client.containers.get(container_id)
            container.stop()
            container.reload()
            return {
                "id": container.short_id,
                "name": container.name,
                "status": container.status,
                "stopped": True,
            }

        elif tool_name == "remove_container":
            container_id = params.get("container_id", "")
            force = params.get("force", False)
            if not container_id:
                return {"error": "container_id parameter is required"}
            container = client.containers.get(container_id)
            container.remove(force=force)
            return {"id": container_id, "removed": True}

        elif tool_name == "container_logs":
            container_id = params.get("container_id", "")
            tail = params.get("tail", 100)
            if not container_id:
                return {"error": "container_id parameter is required"}
            container = client.containers.get(container_id)
            logs = container.logs(tail=tail).decode("utf-8", errors="replace")
            return {"id": container.short_id, "logs": logs[:20000]}

        elif tool_name == "run_container":
            image = params.get("image", "")
            command = params.get("command", "")
            if not image:
                return {"error": "image parameter is required"}
            result = client.containers.run(image, command, remove=True, stdout=True, stderr=True)
            output = result.decode("utf-8", errors="replace") if isinstance(result, bytes) else str(result)
            return {"output": output[:20000]}

        else:
            return {"error": f"Unknown Docker tool: {tool_name}"}

    except docker.errors.NotFound as e:
        return {"error": f"Docker resource not found: {str(e)}"}
    except docker.errors.APIError as e:
        return {"error": f"Docker API error: {str(e)}"}
    except Exception as e:
        return {"error": f"Docker error: {str(e)}"}
