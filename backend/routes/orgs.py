from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
import uuid
import json

router = APIRouter()


# ─── Pydantic Models ───

class OrgCreate(BaseModel):
    name: str
    description: Optional[str] = ""

class OrgUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

class WorkspaceCreate(BaseModel):
    name: str
    description: Optional[str] = ""

class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


# ─── Organization CRUD ───

@router.get("")
async def list_orgs(x_user_email: str = Header(...)):
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT o.*, 
                (SELECT COUNT(*) FROM environments WHERE org_id = o.id) as environment_count,
                (SELECT COUNT(*) FROM workspaces WHERE org_id = o.id) as workspace_count,
                om.role, om.status
            FROM organizations o
            JOIN organization_members om ON o.id = om.org_id
            WHERE om.user_email = ? AND om.status IN ('active', 'pending')
            ORDER BY o.created_at DESC
        """, (x_user_email,))
        rows = await cursor.fetchall()
        return {"organizations": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.post("")
async def create_org(org: OrgCreate, x_user_email: str = Header(...)):
    db = await get_db()
    try:
        org_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        await db.execute(
            "INSERT INTO organizations (id, name, description, owner_email, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (org_id, org.name, org.description, x_user_email, now, now)
        )

        member_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO organization_members (id, org_id, user_email, role, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, org_id, x_user_email, 'owner', 'active', now, now)
        )

        # Auto-create a default environment
        env_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO environments (id, org_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (env_id, org_id, "Default Environment", "Auto-created with organization", now, now)
        )

        # Auto-create a default workspace in the default environment
        ws_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO workspaces (id, org_id, env_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws_id, org_id, env_id, "Default Workspace", "Auto-created with organization", now, now)
        )

        await db.commit()
        return {"id": org_id, "name": org.name, "default_env_id": env_id, "default_workspace_id": ws_id}
    finally:
        await db.close()


@router.get("/{org_id}")
async def get_org(org_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM organizations WHERE id = ?", (org_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Organization not found")
        return dict(row)
    finally:
        await db.close()


@router.put("/{org_id}")
async def update_org(org_id: str, update: OrgUpdate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM organizations WHERE id = ?", (org_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Organization not found")

        _ALLOWED_FIELDS = {"name", "description"}
        field_values = {}
        if update.name is not None:
            field_values["name"] = update.name
        if update.description is not None:
            field_values["description"] = update.description

        if field_values:
            field_values["updated_at"] = datetime.utcnow().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in field_values)
            params = list(field_values.values()) + [org_id]
            await db.execute(f"UPDATE organizations SET {set_clause} WHERE id = ?", params)
            await db.commit()

        return {"status": "updated"}
    finally:
        await db.close()


@router.delete("/{org_id}")
async def delete_org(org_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM organizations WHERE id = ?", (org_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Organization not found")

        await db.execute("DELETE FROM organizations WHERE id = ?", (org_id,))
        await db.commit()
        return {"status": "deleted"}
    finally:
        await db.close()


# ─── Workspace CRUD (nested under org) ───

@router.get("/{org_id}/workspaces")
async def list_workspaces(org_id: str, env_id: Optional[str] = None):
    db = await get_db()
    try:
        if env_id:
            cursor = await db.execute(
                "SELECT * FROM workspaces WHERE org_id = ? AND env_id = ? ORDER BY created_at",
                (org_id, env_id)
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM workspaces WHERE org_id = ? ORDER BY created_at",
                (org_id,)
            )
        rows = await cursor.fetchall()
        return {"workspaces": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.post("/{org_id}/workspaces")
async def create_workspace(org_id: str, ws: WorkspaceCreate, env_id: Optional[str] = None):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM organizations WHERE id = ?", (org_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Organization not found")

        # If no env_id provided, use the first environment of the org
        if not env_id:
            cursor = await db.execute(
                "SELECT id FROM environments WHERE org_id = ? ORDER BY created_at LIMIT 1",
                (org_id,)
            )
            row = await cursor.fetchone()
            if row:
                env_id = row["id"]

        ws_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        await db.execute(
            "INSERT INTO workspaces (id, org_id, env_id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ws_id, org_id, env_id, ws.name, ws.description, now, now)
        )
        await db.commit()
        return {"id": ws_id, "name": ws.name, "org_id": org_id, "env_id": env_id}
    finally:
        await db.close()


@router.put("/{org_id}/workspaces/{ws_id}")
async def update_workspace(org_id: str, ws_id: str, update: WorkspaceUpdate):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM workspaces WHERE id = ? AND org_id = ?", (ws_id, org_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Workspace not found")

        _ALLOWED_FIELDS = {"name", "description"}
        field_values = {}
        if update.name is not None:
            field_values["name"] = update.name
        if update.description is not None:
            field_values["description"] = update.description

        if field_values:
            field_values["updated_at"] = datetime.utcnow().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in field_values)
            params = list(field_values.values()) + [ws_id]
            await db.execute(f"UPDATE workspaces SET {set_clause} WHERE id = ?", params)
            await db.commit()

        return {"status": "updated"}
    finally:
        await db.close()


@router.delete("/{org_id}/workspaces/{ws_id}")
async def delete_workspace(org_id: str, ws_id: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM workspaces WHERE id = ? AND org_id = ?", (ws_id, org_id)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Workspace not found")

        await db.execute("DELETE FROM workspaces WHERE id = ?", (ws_id,))
        await db.commit()
        return {"status": "deleted"}
    finally:
        await db.close()
