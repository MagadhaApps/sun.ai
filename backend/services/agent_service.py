import json
import uuid
import time
from datetime import datetime
from database import get_db
from services.chat_service import stream_chat_completion, non_stream_chat_completion
from services.tool_service import execute_tool, get_tool_schema_for_llm
from services.tool_service import execute_tool, get_tool_schema_for_llm
from services.observability_service import log_llm_call
from services.mcp_service import execute_mcp_tool
from services.knowledge_service import search_knowledge


def _clean_azure_base_url(provider_type: str, base_url: str) -> str:
    """Clean Azure base URL: strip paths, query params, convert cognitiveservices domain."""
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


async def get_agent_config(agent_id: str):
    """Load agent configuration from database."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        agent = dict(row)
        agent["tools"] = json.loads(agent.get("tools") or "[]")
        agent["mcp_servers"] = json.loads(agent.get("mcp_servers") or "[]")
        agent["skills"] = json.loads(agent.get("skills") or "[]")
        agent["knowledge_bases"] = json.loads(agent.get("knowledge_bases") or "[]")

        # Resolve workspace scope context for secrets access
        workspace_id = agent.get("workspace_id") or "default-workspace"
        scope_context = {
            "workspace_id": workspace_id,
            "environment_id": "default-env",
            "org_id": "default-org",
        }
        # Try to resolve actual env and org from workspace
        cursor = await db.execute(
            "SELECT org_id, env_id FROM workspaces WHERE id = ?", (workspace_id,)
        )
        ws_row = await cursor.fetchone()
        if ws_row:
            scope_context["org_id"] = ws_row["org_id"] or "default-org"
            scope_context["environment_id"] = ws_row["env_id"] or "default-env"
        agent["scope_context"] = scope_context

        # Load provider info
        if agent.get("provider_id"):
            cursor = await db.execute("SELECT * FROM providers WHERE id = ?", (agent["provider_id"],))
            prow = await cursor.fetchone()
            if prow:
                agent["provider"] = dict(prow)

        # Inject Skills into System Prompt
        skills_content = ""
        for skill_id in agent["skills"]:
            sc = await db.execute("SELECT name, content FROM skills WHERE id = ?", (skill_id,))
            srow = await sc.fetchone()
            if srow:
                skills_content += f"\n\n--- Skill: {srow['name']} ---\n{srow['content']}"
        
        if skills_content:
            base_prompt = agent.get("system_prompt", "")
            agent["system_prompt"] = f"{base_prompt}\n\n# Your Skills\nYou have been granted the following specific skills and instructions. Follow them strictly:{skills_content}".strip()

        # Resolve model UUID → actual model_id string (e.g. "llama-3.3-70b-versatile")
        if agent.get("model_id"):
            cursor = await db.execute("SELECT model_id FROM models WHERE id = ?", (agent["model_id"],))
            mrow = await cursor.fetchone()
            if mrow:
                agent["model_id"] = mrow["model_id"]

        # Load tool schemas
        tool_schemas = []
        tool_map = {}
        for tool_id in agent["tools"]:
            tc = await db.execute("SELECT * FROM tools WHERE id = ? AND is_enabled = 1", (tool_id,))
            tool_row = await tc.fetchone()
            if tool_row:
                tool = dict(tool_row)
                tool["parameters_schema"] = json.loads(tool.get("parameters_schema", "{}"))
                tool_schemas.append(get_tool_schema_for_llm(tool))
                tool_map[tool["name"]] = tool

        # Load MCP server tools — merge into the same tool_schemas list
        mcp_tool_ids = set()  # Track which tool names come from MCP servers
        mcp_server_map = {}   # tool_name -> server_id for routing
        for mcp_id in agent.get("mcp_servers", []):
            mc = await db.execute("SELECT * FROM mcp_servers WHERE id = ?", (mcp_id,))
            mcp_row = await mc.fetchone()
            if mcp_row:
                server = dict(mcp_row)
                available = json.loads(server.get("available_tools", "[]"))
                for mcp_tool in available:
                    # Prefix with server name to avoid collisions
                    qualified_name = f"{server['name']}__{mcp_tool['name']}"
                    schema = {
                        "type": "function",
                        "function": {
                            "name": qualified_name,
                            "description": f"[{server['name']}] {mcp_tool.get('description', '')}",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    k: {"type": v, "description": k}
                                    for k, v in mcp_tool.get("parameters", {}).items()
                                },
                                "required": list(mcp_tool.get("parameters", {}).keys()),
                            }
                        }
                    }
                    tool_schemas.append(schema)
                    mcp_tool_ids.add(qualified_name)
                    mcp_server_map[qualified_name] = {
                        "server_id": mcp_id,
                        "tool_name": mcp_tool["name"],
                        "server_name": server["name"],
                    }

        agent["tool_schemas"] = tool_schemas
        agent["tool_map"] = tool_map
        agent["mcp_tool_ids"] = mcp_tool_ids
        agent["mcp_server_map"] = mcp_server_map

        # Inject Native Knowledge Base Semantic Search Tool natively if KBs are attached
        if agent["knowledge_bases"]:
            agent["has_knowledge_tool"] = True
            agent["tool_schemas"].append({
                "type": "function",
                "function": {
                    "name": "search_knowledge_base",
                    "description": "Semantic search across your connected Knowledge Bases to find relevant documents and information.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The search query to match against the vector database."},
                        },
                        "required": ["query"],
                    }
                }
            })
        else:
            agent["has_knowledge_tool"] = False

        return agent
    finally:
        await db.close()


from typing import Any

async def run_agent(agent_id: str, query: Any, conversation_id: str = None):
    """Run agent with agentic tool-calling loop (non-streaming)."""
    agent = await get_agent_config(agent_id)
    if not agent:
        return {"error": "Agent not found"}
    if not agent.get("provider"):
        return {"error": "Agent has no provider configured"}

    provider = agent["provider"]
    messages = []
    if agent.get("system_prompt"):
        messages.append({"role": "system", "content": agent["system_prompt"]})

    # Load history if conversation_id exists
    if conversation_id:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT role, content, tool_call_id, tool_calls FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,)
            )
            rows = await cursor.fetchall()
            for r in rows:
                content = r["content"] if r["content"] is not None else ""
                if content and isinstance(content, str) and content.startswith("["):
                    try:
                        content = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                msg = {"role": r["role"], "content": content}
                if r["tool_calls"]:
                    msg["tool_calls"] = json.loads(r["tool_calls"])
                if r["tool_call_id"]:
                    msg["tool_call_id"] = r["tool_call_id"]
                messages.append(msg)
        finally:
            await db.close()
    else:
        messages.append({"role": "user", "content": query})

    steps = []
    iteration = 0
    max_iter = agent.get("max_iterations", 10)

    scope_ctx = agent.get("scope_context", {})

    while iteration < max_iter:
        iteration += 1

        try:
            result = await non_stream_chat_completion(
                provider_type=provider["type"],
                model_id=agent["model_id"],
                messages=messages,
                api_key=provider["api_key_encrypted"],
                base_url=_clean_azure_base_url(provider["type"], provider.get("base_url")),
                api_version=provider.get("api_version"),
                tools=agent["tool_schemas"] if agent["tool_schemas"] else None,
                temperature=agent.get("temperature", 0.7),
                max_tokens=agent.get("max_tokens", 4096),
                provider_id=provider["id"],
                provider_name=provider["name"],
                conversation_id=conversation_id,
                source="agent",
                org_id=scope_ctx.get("org_id"),
                workspace_id=scope_ctx.get("workspace_id"),
            )
        except Exception as llm_err:
            err_str = str(llm_err)
            # If provider failed on tool calling, retry without tools for a plain answer
            if "tool_use_failed" in err_str or "failed_generation" in err_str:
                steps.append({"type": "tool_error", "content": "Model failed to use tools — retrying without tools", "iteration": iteration})
                try:
                    result = await non_stream_chat_completion(
                        provider_type=provider["type"],
                        model_id=agent["model_id"],
                        messages=messages,
                        api_key=provider["api_key_encrypted"],
                        base_url=_clean_azure_base_url(provider["type"], provider.get("base_url")),
                        api_version=provider.get("api_version"),
                        tools=None,
                        temperature=agent.get("temperature", 0.7),
                        max_tokens=agent.get("max_tokens", 4096),
                        provider_id=provider["id"],
                        provider_name=provider["name"],
                        conversation_id=conversation_id,
                        source="agent",
                        org_id=scope_ctx.get("org_id"),
                        workspace_id=scope_ctx.get("workspace_id"),
                    )
                except Exception as retry_err:
                    return {"error": f"LLM error: {str(retry_err)}", "steps": steps}
            else:
                return {"error": f"LLM error: {err_str}", "steps": steps}

        if result.get("tool_calls"):
            # LLM wants to use tools — execute them and loop
            # Ensure each tool_call has 'type' field (required by Groq and some providers)
            normalized_tcs = []
            for tc in result["tool_calls"]:
                ntc = dict(tc)
                if "type" not in ntc:
                    ntc["type"] = "function"
                normalized_tcs.append(ntc)
            assistant_msg = {
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": normalized_tcs,
            }
            messages.append(assistant_msg)

            for tc in result["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                step = {"type": "tool_call", "tool": fn_name, "arguments": fn_args, "iteration": iteration}
                steps.append(step)

                # Execute the tool — route to MCP or builtin
                scope_ctx = agent.get("scope_context", {})
                try:
                    if fn_name == "search_knowledge_base" and agent.get("has_knowledge_tool"):
                        tool_result = await search_knowledge(agent["knowledge_bases"], fn_args.get("query", ""))
                    elif fn_name in agent.get("mcp_tool_ids", set()):
                        mcp_info = agent["mcp_server_map"][fn_name]
                        tool_result = await execute_mcp_tool(
                            mcp_info["server_id"], mcp_info["tool_name"], fn_args,
                            org_id=scope_ctx.get("org_id"),
                        )
                    else:
                        tool_data = agent["tool_map"].get(fn_name)
                        code = tool_data.get("code") if tool_data else None
                        # Pass scope context for get_secret() access in custom tools
                        tool_result = await execute_tool(fn_name, fn_args, code, scope_ctx)
                    tool_result_str = json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result
                except Exception as e:
                    tool_result_str = json.dumps({"error": str(e)})

                steps.append({"type": "tool_result", "tool": fn_name, "result": tool_result_str[:5000], "iteration": iteration})

                messages.append({
                    "role": "tool",
                    "content": tool_result_str,
                    "tool_call_id": tc["id"],
                })
        else:
            # Final answer — no more tool calls
            steps.append({"type": "final_answer", "content": result.get("content", ""), "iteration": iteration})
            return {
                "content": result.get("content", ""),
                "steps": steps,
                "iterations": iteration,
                "usage": result.get("usage"),
                "conversation_id": conversation_id,
            }

    # Hit max iterations
    return {
        "content": "Agent reached maximum iterations without a final answer.",
        "steps": steps,
        "iterations": iteration,
        "conversation_id": conversation_id,
    }


async def stream_agent(agent_id: str, query: Any, conversation_id: str = None):
    """Run agent with agentic loop, yielding SSE events for each step."""
    agent = await get_agent_config(agent_id)
    if not agent:
        yield {"type": "error", "error": "Agent not found"}
        return
    if not agent.get("provider"):
        yield {"type": "error", "error": "Agent has no provider configured"}
        return

    provider = agent["provider"]
    messages = []
    if agent.get("system_prompt"):
        messages.append({"role": "system", "content": agent["system_prompt"]})

    # Load history if conversation_id exists
    if conversation_id:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT role, content, tool_call_id, tool_calls FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,)
            )
            rows = await cursor.fetchall()
            for r in rows:
                content = r["content"] if r["content"] is not None else ""
                if content and isinstance(content, str) and content.startswith("["):
                    try:
                        content = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        pass
                        
                msg = {"role": r["role"], "content": content}
                if r["tool_calls"]:
                    msg["tool_calls"] = json.loads(r["tool_calls"])
                if r["tool_call_id"]:
                    msg["tool_call_id"] = r["tool_call_id"]
                messages.append(msg)
        finally:
            await db.close()
    else:
        messages.append({"role": "user", "content": query})

    iteration = 0
    max_iter = agent.get("max_iterations", 10)
    scope_ctx = agent.get("scope_context", {})

    while iteration < max_iter:
        iteration += 1
        yield {"type": "thinking", "iteration": iteration}

        try:
            result = await non_stream_chat_completion(
                provider_type=provider["type"],
                model_id=agent["model_id"],
                messages=messages,
                api_key=provider["api_key_encrypted"],
                base_url=_clean_azure_base_url(provider["type"], provider.get("base_url")),
                api_version=provider.get("api_version"),
                tools=agent["tool_schemas"] if agent["tool_schemas"] else None,
                temperature=agent.get("temperature", 0.7),
                max_tokens=agent.get("max_tokens", 4096),
                provider_id=provider["id"],
                provider_name=provider["name"],
                conversation_id=conversation_id,
                source="agent",
                org_id=scope_ctx.get("org_id"),
                workspace_id=scope_ctx.get("workspace_id"),
            )
        except Exception as llm_err:
            err_str = str(llm_err)
            if "tool_use_failed" in err_str or "failed_generation" in err_str:
                yield {"type": "tool_error", "content": "Model failed to use tools — retrying without tools", "iteration": iteration}
                try:
                    result = await non_stream_chat_completion(
                        provider_type=provider["type"],
                        model_id=agent["model_id"],
                        messages=messages,
                        api_key=provider["api_key_encrypted"],
                        base_url=_clean_azure_base_url(provider["type"], provider.get("base_url")),
                        api_version=provider.get("api_version"),
                        tools=None,
                        temperature=agent.get("temperature", 0.7),
                        max_tokens=agent.get("max_tokens", 4096),
                        provider_id=provider["id"],
                        provider_name=provider["name"],
                        conversation_id=conversation_id,
                        source="agent",
                        org_id=scope_ctx.get("org_id"),
                        workspace_id=scope_ctx.get("workspace_id"),
                    )
                except Exception as retry_err:
                    yield {"type": "error", "error": f"LLM error: {str(retry_err)}"}
                    return
            else:
                yield {"type": "error", "error": f"LLM error: {err_str}"}
                return

        if result.get("tool_calls"):
            normalized_tcs = []
            for tc in result["tool_calls"]:
                ntc = dict(tc)
                if "type" not in ntc:
                    ntc["type"] = "function"
                normalized_tcs.append(ntc)
            assistant_msg = {
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": normalized_tcs,
            }
            messages.append(assistant_msg)

            for tc in result["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                yield {"type": "tool_call", "tool": fn_name, "arguments": fn_args, "iteration": iteration}

                scope_ctx = agent.get("scope_context", {})
                try:
                    if fn_name == "search_knowledge_base" and agent.get("has_knowledge_tool"):
                        tool_result = await search_knowledge(agent["knowledge_bases"], fn_args.get("query", ""))
                    elif fn_name in agent.get("mcp_tool_ids", set()):
                        mcp_info = agent["mcp_server_map"][fn_name]
                        tool_result = await execute_mcp_tool(
                            mcp_info["server_id"], mcp_info["tool_name"], fn_args,
                            org_id=scope_ctx.get("org_id"),
                        )
                    else:
                        tool_data = agent["tool_map"].get(fn_name)
                        code = tool_data.get("code") if tool_data else None
                        tool_result = await execute_tool(fn_name, fn_args, code, scope_ctx)
                    tool_result_str = json.dumps(tool_result) if not isinstance(tool_result, str) else tool_result
                except Exception as e:
                    tool_result_str = json.dumps({"error": str(e)})

                yield {"type": "tool_result", "tool": fn_name, "result": tool_result_str[:3000], "iteration": iteration}

                messages.append({
                    "role": "tool",
                    "content": tool_result_str,
                    "tool_call_id": tc["id"],
                })
        else:
            yield {"type": "content", "content": result.get("content", ""), "iteration": iteration}
            yield {"type": "done", "iterations": iteration, "usage": result.get("usage"), "conversation_id": conversation_id}
            return

    yield {"type": "content", "content": "Agent reached maximum iterations.", "iteration": iteration}
    yield {"type": "done", "iterations": iteration, "conversation_id": conversation_id}
