from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from database import init_db

from routes.providers import router as providers_router
from routes.tools import router as tools_router
from routes.mcp import router as mcp_router

from routes.chat import router as chat_router
from routes.workflows import router as workflows_router
from routes.observability import router as observability_router
from routes.agents import router as agents_router
from routes.orgs import router as orgs_router
from routes.environments import router as environments_router
from routes.secrets import router as secrets_router
from routes.members import router as members_router
from routes.permissions import router as permissions_router
from routes.skills import router as skills_router
from routes.knowledge import router as knowledge_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Seed built-in tools
    from services.tool_service import seed_builtin_tools
    await seed_builtin_tools()
    # Seed built-in MCP servers
    from services.mcp_service import seed_builtin_mcp_servers
    await seed_builtin_mcp_servers()
    yield

app = FastAPI(
    title="Agentic AI Platform",
    description="A comprehensive AI agent platform with providers, tools, MCP servers, workflows, and observability",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(providers_router, prefix="/api/providers", tags=["providers"])
app.include_router(tools_router, prefix="/api/tools", tags=["tools"])
app.include_router(mcp_router, prefix="/api/mcp", tags=["mcp"])

app.include_router(chat_router, prefix="/api/chat", tags=["chat"])
app.include_router(workflows_router, prefix="/api/workflows", tags=["workflows"])
app.include_router(observability_router, prefix="/api/observability", tags=["observability"])
app.include_router(agents_router, prefix="/api/agents", tags=["agents"])
app.include_router(orgs_router, prefix="/api/orgs", tags=["organizations"])
app.include_router(environments_router, prefix="/api/orgs", tags=["environments"])
app.include_router(secrets_router, prefix="/api/secrets", tags=["secrets"])
app.include_router(members_router, prefix="/api/orgs/{org_id}/members", tags=["members"])
app.include_router(permissions_router, prefix="/api/permissions", tags=["permissions"])
app.include_router(skills_router, prefix="/api/skills", tags=["skills"])
app.include_router(knowledge_router, prefix="/api/knowledge", tags=["knowledge"])

@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
