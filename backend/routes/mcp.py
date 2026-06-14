from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from services.mcp_service import start_mcp_server, stop_mcp_server, get_server_status, execute_mcp_tool, discover_mcp_tools
import uuid
import json

router = APIRouter()

class MCPServerCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    command: str
    args: Optional[list] = []
    env: Optional[dict] = {}
    config: Optional[dict] = {}

class MCPServerUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list] = None
    env: Optional[dict] = None
    config: Optional[dict] = None

class MCPToolExec(BaseModel):
    tool_name: str
    parameters: dict = {}


@router.get("")
async def list_mcp_servers(workspace_id: Optional[str] = None):
    db = await get_db()
    try:
        if workspace_id:
            cursor = await db.execute("SELECT * FROM mcp_servers WHERE workspace_id = ? ORDER BY type, name", (workspace_id,))
        else:
            cursor = await db.execute("SELECT * FROM mcp_servers ORDER BY type, name")
        rows = await cursor.fetchall()
        servers = []
        for row in rows:
            s = dict(row)
            s["args"] = json.loads(s.get("args", "[]"))
            s["env"] = json.loads(s.get("env", "{}"))
            s["available_tools"] = json.loads(s.get("available_tools", "[]"))
            s["config"] = json.loads(s.get("config", "{}"))
            # Update live status
            s["status"] = await get_server_status(s["id"])
            servers.append(s)
        return {"servers": servers, "count": len(servers)}
    finally:
        await db.close()


@router.get("/running")
async def list_running_servers():
    """Return only MCP servers whose live status is 'running'."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers ORDER BY type, name")
        rows = await cursor.fetchall()
        running = []
        for row in rows:
            s = dict(row)
            live_status = await get_server_status(s["id"])
            if live_status != "running":
                continue
            s["args"] = json.loads(s.get("args", "[]"))
            s["env"] = json.loads(s.get("env", "{}"))
            s["available_tools"] = json.loads(s.get("available_tools", "[]"))
            s["config"] = json.loads(s.get("config", "{}"))
            s["status"] = live_status
            running.append(s)
        return {"servers": running, "count": len(running)}
    finally:
        await db.close()


@router.post("")
async def create_mcp_server(server: MCPServerCreate):
    server_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO mcp_servers (id, name, type, command, args, env, status, description, config, created_at, updated_at)
               VALUES (?, ?, 'custom', ?, ?, ?, 'stopped', ?, ?, ?, ?)""",
            (server_id, server.name, server.command, json.dumps(server.args),
             json.dumps(server.env), server.description, json.dumps(server.config), now, now)
        )
        await db.commit()
        return {"id": server_id, "name": server.name, "type": "custom", "status": "stopped", "created_at": now}
    finally:
        await db.close()


@router.get("/{server_id}")
async def get_mcp_server(server_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        s = dict(row)
        s["args"] = json.loads(s.get("args", "[]"))
        s["env"] = json.loads(s.get("env", "{}"))
        s["available_tools"] = json.loads(s.get("available_tools", "[]"))
        s["config"] = json.loads(s.get("config", "{}"))
        s["status"] = await get_server_status(server_id)
        return s
    finally:
        await db.close()


@router.put("/{server_id}")
async def update_mcp_server(server_id: str, update: MCPServerUpdate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        existing = dict(row)
        if existing["type"] == "builtin":
            # Builtin servers allow only env and config updates (for API keys etc.)
            if any([update.name, update.command, update.args is not None]):
                raise HTTPException(
                    status_code=400,
                    detail="Built-in servers only allow env and config updates"
                )
            if update.env is None and update.config is None and update.description is None:
                return {"id": server_id, "message": "nothing to update"}
            now = datetime.utcnow().isoformat()
            await db.execute(
                """UPDATE mcp_servers
                   SET env = ?, config = ?, description = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(update.env) if update.env is not None else existing["env"],
                    json.dumps(update.config) if update.config is not None else existing["config"],
                    update.description if update.description is not None else existing["description"],
                    now,
                    server_id,
                ),
            )
        else:
            now = datetime.utcnow().isoformat()
            await db.execute(
                """UPDATE mcp_servers SET name=?, description=?, command=?, args=?, env=?, config=?, updated_at=?
                   WHERE id=?""",
                (update.name or existing["name"],
                 update.description if update.description is not None else existing["description"],
                 update.command or existing["command"],
                 json.dumps(update.args) if update.args is not None else existing["args"],
                 json.dumps(update.env) if update.env is not None else existing["env"],
                 json.dumps(update.config) if update.config is not None else existing["config"],
                 now, server_id)
            )
        await db.commit()
        return {"id": server_id, "updated_at": now}
    finally:
        await db.close()


@router.delete("/{server_id}")
async def delete_mcp_server(server_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        if dict(row)["type"] == "builtin":
            raise HTTPException(status_code=400, detail="Cannot delete built-in MCP servers")
        await stop_mcp_server(server_id)
        await db.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        await db.commit()
        return {"deleted": True}
    finally:
        await db.close()


@router.post("/{server_id}/start")
async def start_server(server_id: str, org_id: Optional[str] = None):
    result = await start_mcp_server(server_id, org_id=org_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{server_id}/discover")
async def discover_tools(server_id: str, org_id: Optional[str] = None):
    """Discover available tools from an MCP server."""
    result = await discover_mcp_tools(server_id, org_id=org_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{server_id}/stop")
async def stop_server(server_id: str):
    result = await stop_mcp_server(server_id)
    return result


@router.post("/{server_id}/restart")
async def restart_server(server_id: str, org_id: Optional[str] = None):
    await stop_mcp_server(server_id)
    import asyncio
    await asyncio.sleep(1)
    result = await start_mcp_server(server_id, org_id=org_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/{server_id}/tools")
async def get_server_tools(server_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT available_tools FROM mcp_servers WHERE id = ?", (server_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="MCP server not found")
        tools = json.loads(row["available_tools"])
        return {"tools": tools, "count": len(tools)}
    finally:
        await db.close()


@router.post("/{server_id}/execute")
async def execute_tool_on_server(server_id: str, exec_req: MCPToolExec):
    import time
    start = time.time()
    result = await execute_mcp_tool(server_id, exec_req.tool_name, exec_req.parameters)
    elapsed = int((time.time() - start) * 1000)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return {"result": result, "execution_time_ms": elapsed}


@router.get("/{server_id}/status")
async def check_server_status(server_id: str):
    status = await get_server_status(server_id)
    return {"status": status}
