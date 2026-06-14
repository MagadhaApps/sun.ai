from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from auth import RequirePermission
import uuid

router = APIRouter()


# ─── Pydantic Models ───

class EnvCreate(BaseModel):
    name: str
    description: Optional[str] = ""

class EnvUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


# ─── Environment CRUD ───

@router.get("/{org_id}/environments")
async def list_environments(org_id: str, auth: dict = Depends(RequirePermission())):
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT e.*,
                (SELECT COUNT(*) FROM workspaces WHERE env_id = e.id) as workspace_count
            FROM environments e WHERE e.org_id = ? ORDER BY e.created_at
        """, (org_id,))
        rows = await cursor.fetchall()
        return {"environments": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.post("/{org_id}/environments")
async def create_environment(org_id: str, env: EnvCreate, auth: dict = Depends(RequirePermission('admin'))):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM organizations WHERE id = ?", (org_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Organization not found")

        env_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        await db.execute(
            "INSERT INTO environments (id, org_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (env_id, org_id, env.name, env.description, now, now)
        )

        # Auto-create a default workspace in this environment
        ws_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workspaces (id, org_id, env_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws_id, org_id, env_id, "Default Workspace", "Auto-created with environment", now, now)
        )

        await db.commit()
        return {"id": env_id, "name": env.name, "default_workspace_id": ws_id}
    finally:
        await db.close()


@router.put("/{org_id}/environments/{env_id}")
async def update_environment(org_id: str, env_id: str, update: EnvUpdate, auth: dict = Depends(RequirePermission('admin'))):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM environments WHERE id = ? AND org_id = ?", (env_id, org_id)
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Environment not found")

        if update.name is not None or update.description is not None:
            now = datetime.utcnow().isoformat()
            await db.execute(
                "UPDATE environments SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                (
                    update.name if update.name is not None else existing["name"],
                    update.description if update.description is not None else existing["description"],
                    now,
                    env_id,
                ),
            )
            await db.commit()

        return {"status": "updated"}
    finally:
        await db.close()


@router.delete("/{org_id}/environments/{env_id}")
async def delete_environment(org_id: str, env_id: str, auth: dict = Depends(RequirePermission('admin'))):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM environments WHERE id = ? AND org_id = ?", (env_id, org_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Environment not found")

        await db.execute("DELETE FROM environments WHERE id = ?", (env_id,))
        await db.commit()
        return {"status": "deleted"}
    finally:
        await db.close()
