from fastapi import APIRouter, HTTPException, Header, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from database import get_db
from services.chat_service import stream_chat_completion, non_stream_chat_completion
from services.tool_service import execute_tool, get_tool_schema_for_llm
from services.document_parser import parse_document
import uuid
import json
import asyncio
import os
import tempfile


router = APIRouter()


def _clean_azure_base_url(base_url: str) -> str:
    """Extract clean Azure endpoint from potentially full URL."""
    if not base_url:
        return base_url
    azure_url = base_url.rstrip("/")
    # If URL contains /openai/, extract just the base endpoint
    if "/openai/" in azure_url:
        azure_url = azure_url.split("/openai/")[0]
    # Remove query parameters if present
    if "?" in azure_url:
        azure_url = azure_url.split("?")[0]
    # Convert cognitiveservices.azure.com to openai.azure.com
    if "cognitiveservices.azure.com" in azure_url:
        azure_url = azure_url.replace("cognitiveservices.azure.com", "openai.azure.com")
    return azure_url

class ChatMessage(BaseModel):
    role: str
    content: Any  # Supports string or list of dicts (for vision)
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list] = None

class ChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    workspace_id: Optional[str] = None
    provider_id: str
    model_id: str
    messages: List[ChatMessage]
    system_prompt: Optional[str] = None
    tools: Optional[List[str]] = []
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 4096
    stream: Optional[bool] = True

class ConversationCreate(BaseModel):
    title: str
    workspace_id: Optional[str] = None
    model_id: Optional[str] = None
    provider_id: Optional[str] = None
    system_prompt: Optional[str] = None


@router.post("/completions")
async def chat_completions(request: ChatRequest, x_user_email: str = Header(None)):
    db = await get_db()
    try:
        # Get provider info
        cursor = await db.execute("SELECT * FROM providers WHERE id = ?", (request.provider_id,))
        provider = await cursor.fetchone()
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")
        provider = dict(provider)

        # Build messages
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        for msg in request.messages:
            m = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            messages.append(m)

        # Get tool schemas if tools requested
        tool_schemas = None
        if request.tools:
            tool_schemas = []
            for tool_id in request.tools:
                tc = await db.execute("SELECT * FROM tools WHERE id = ? AND is_enabled = 1", (tool_id,))
                tool_row = await tc.fetchone()
                if tool_row:
                    tool = dict(tool_row)
                    tool["parameters_schema"] = json.loads(tool.get("parameters_schema", "{}"))
                    tool_schemas.append(get_tool_schema_for_llm(tool))

        # Create or update conversation
        conv_id = request.conversation_id
        if not conv_id:
            conv_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            await db.execute(
                """INSERT INTO conversations (id, workspace_id, user_email, title, model_id, provider_id, system_prompt, tools, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (conv_id, request.workspace_id, x_user_email, request.messages[0].content[:50] + "..." if request.messages else "New Chat",
                 request.model_id, request.provider_id, request.system_prompt, json.dumps(request.tools or []), now, now)
            )
            await db.commit()

        user_msg = request.messages[-1] if request.messages else None
        if user_msg and user_msg.role == "user":
            msg_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            
            # Serialize content to JSON string if it's a list (vision format)
            content_to_save = json.dumps(user_msg.content) if isinstance(user_msg.content, list) else str(user_msg.content)
            
            await db.execute(
                """INSERT INTO messages (id, conversation_id, role, content, model_id, created_at)
                   VALUES (?, ?, 'user', ?, ?, ?)""",
                (msg_id, conv_id, content_to_save, request.model_id, now)
            )
            await db.commit()

    finally:
        await db.close()

    if request.stream:
        async def event_stream():
            full_content = ""
            async for chunk in stream_chat_completion(
                provider_type=provider["type"],
                model_id=request.model_id,
                messages=messages,
                api_key=provider["api_key_encrypted"],
                base_url=_clean_azure_base_url(provider.get("base_url")),
                api_version=provider.get("api_version"),
                tools=tool_schemas,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                provider_id=provider["id"],
                provider_name=provider["name"],
                conversation_id=conv_id,
                org_id=provider.get("org_id"),
            ):
                if chunk["type"] == "content":
                    full_content += chunk["content"]
                yield f"data: {json.dumps(chunk)}\n\n"

            # Save assistant message
            if full_content:
                db2 = await get_db()
                try:
                    amsg_id = str(uuid.uuid4())
                    now = datetime.utcnow().isoformat()
                    await db2.execute(
                        """INSERT INTO messages (id, conversation_id, role, content, model_id, created_at)
                           VALUES (?, ?, 'assistant', ?, ?, ?)""",
                        (amsg_id, conv_id, full_content, request.model_id, now)
                    )
                    await db2.commit()
                finally:
                    await db2.close()

            yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    else:
        result = await non_stream_chat_completion(
            provider_type=provider["type"],
            model_id=request.model_id,
            messages=messages,
            api_key=provider["api_key_encrypted"],
            base_url=_clean_azure_base_url(provider.get("base_url")),
            api_version=provider.get("api_version"),
            tools=tool_schemas,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider_id=provider["id"],
            provider_name=provider["name"],
            conversation_id=conv_id,
            org_id=provider.get("org_id"),
        )

        # Save assistant message
        db2 = await get_db()
        try:
            amsg_id = str(uuid.uuid4())
            now = datetime.utcnow().isoformat()
            await db2.execute(
                """INSERT INTO messages (id, conversation_id, role, content, model_id, created_at)
                   VALUES (?, ?, 'assistant', ?, ?, ?)""",
                (amsg_id, conv_id, result["content"], request.model_id, now)
            )
            await db2.commit()
        finally:
            await db2.close()

        result["conversation_id"] = conv_id
        return result


@router.get("/conversations")
async def list_conversations(workspace_id: Optional[str] = None, x_user_email: str = Header(None)):
    db = await get_db()
    try:
        query = """
            SELECT c.*, (SELECT COUNT(*) FROM messages WHERE conversation_id = c.id) as message_count
            FROM conversations c 
            WHERE c.id NOT IN (
                SELECT DISTINCT conversation_id 
                FROM observability_logs 
                WHERE source = 'agent' AND conversation_id IS NOT NULL
            )
            AND c.agent_id IS NULL
        """
        params = []
        if workspace_id:
            query += " AND c.workspace_id = ?"
            params.append(workspace_id)
            
        if x_user_email:
            query += " AND c.user_email = ?"
            params.append(x_user_email)
            
        query += " ORDER BY c.updated_at DESC"
        
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        convos = []
        for row in rows:
            c = dict(row)
            c["tools"] = json.loads(c.get("tools", "[]"))
            c["mcp_servers"] = json.loads(c.get("mcp_servers", "[]"))
            convos.append(c)
        return {"conversations": convos}
    finally:
        await db.close()


@router.post("/conversations")
async def create_conversation(conv: ConversationCreate, x_user_email: str = Header(None)):
    conv_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO conversations (id, workspace_id, user_email, title, model_id, provider_id, system_prompt, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (conv_id, conv.workspace_id, x_user_email, conv.title, conv.model_id, conv.provider_id, conv.system_prompt, now, now)
        )
        await db.commit()
        return {"id": conv_id, "title": conv.title, "created_at": now}
    finally:
        await db.close()


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, x_user_email: str = Header(None)):
    db = await get_db()
    try:
        query = "SELECT * FROM conversations WHERE id = ?"
        params = [conv_id]
        if x_user_email:
            query += " AND user_email = ?"
            params.append(x_user_email)
            
        cursor = await db.execute(query, tuple(params))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        c = dict(row)
        c["tools"] = json.loads(c.get("tools", "[]"))
        c["mcp_servers"] = json.loads(c.get("mcp_servers", "[]"))
        return c
    finally:
        await db.close()


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, x_user_email: str = Header(None)):
    db = await get_db()
    try:
        query = "SELECT id FROM conversations WHERE id = ?"
        params = [conv_id]
        if x_user_email:
            query += " AND user_email = ?"
            params.append(x_user_email)
            
        cursor = await db.execute(query, tuple(params))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Conversation not found")
            
        await db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        await db.commit()
        return {"deleted": True}
    finally:
        await db.close()


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str, x_user_email: str = Header(None)):
    db = await get_db()
    try:
        # Check access
        query = "SELECT id FROM conversations WHERE id = ?"
        params = [conv_id]
        if x_user_email:
            query += " AND user_email = ?"
            params.append(x_user_email)
            
        cursor = await db.execute(query, tuple(params))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Conversation not found")
            
        cursor = await db.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conv_id,)
        )
        rows = await cursor.fetchall()
        messages = []
        for row in rows:
            m = dict(row)
            m["tokens_used"] = json.loads(m.get("tokens_used", "{}"))
            if m.get("tool_calls"):
                m["tool_calls"] = json.loads(m["tool_calls"])
            
            # Parse list content (e.g. vision objects) back to Python lists
            if m.get("content") and isinstance(m["content"], str) and m["content"].startswith("["):
                try:
                    m["content"] = json.loads(m["content"])
                except (json.JSONDecodeError, TypeError):
                    pass
                    
            messages.append(m)
        return {"messages": messages}
    finally:
        await db.close()


@router.post("/tools/execute")
async def execute_tool_in_chat(tool_name: str, parameters: dict):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM tools WHERE name = ?", (tool_name,))
        row = await cursor.fetchone()
        code = None
        if row:
            tool = dict(row)
            code = tool.get("code")
    finally:
        await db.close()

    result = await execute_tool(tool_name, parameters, code)
    return {"result": result}


@router.post("/parse_file")
async def parse_chat_file(file: UploadFile = File(...), x_user_email: str = Header(None)):
    """Parse documents uploaded to chat for text extraction."""
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")
        
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
        
    try:
        extracted_text = parse_document(tmp_path, file.filename)
        return {"text": extracted_text, "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
