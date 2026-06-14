from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
import uuid

router = APIRouter()


class SecretCreate(BaseModel):
    scope_type: str = "workspace"  # "org", "env", or "workspace"
    scope_id: str
    name: str
    value: str
    type: str = "secret"  # "secret" or "variable"
    description: Optional[str] = ""

class SecretUpdate(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None
    description: Optional[str] = None


@router.get("")
async def list_secrets(
    scope_type: str = Query(..., description="Scope: org, env, or workspace"),
    scope_id: str = Query(..., description="ID of the scope"),
    include_inherited: bool = Query(True, description="Include inherited secrets from parent scopes"),
):
    """List secrets/variables at a given scope, optionally with inheritance."""
    db = await get_db()
    try:
        results = []
        seen_names = {}  # Track by (name, type) to handle overrides

        # 1) If workspace scope, resolve env_id and org_id for inheritance
        if scope_type == "workspace" and include_inherited:
            cursor = await db.execute(
                "SELECT org_id, env_id FROM workspaces WHERE id = ?", (scope_id,)
            )
            ws_row = await cursor.fetchone()
            if ws_row:
                org_id = ws_row["org_id"]
                env_id = ws_row["env_id"]

                # Get org-level secrets first (lowest priority)
                if org_id:
                    cursor = await db.execute(
                        """SELECT id, scope_type, scope_id, name, type, description,
                                  CASE WHEN type = 'secret' THEN '••••••••' ELSE value_encrypted END as value,
                                  created_at, updated_at
                           FROM secrets WHERE scope_type = 'org' AND scope_id = ? ORDER BY type, name""",
                        (org_id,)
                    )
                    for row in await cursor.fetchall():
                        d = dict(row)
                        d["inherited"] = True
                        d["inherited_from"] = "org"
                        key = (d["name"], d["type"])
                        seen_names[key] = len(results)
                        results.append(d)

                # Get env-level secrets (medium priority, overrides org)
                if env_id:
                    cursor = await db.execute(
                        """SELECT id, scope_type, scope_id, name, type, description,
                                  CASE WHEN type = 'secret' THEN '••••••••' ELSE value_encrypted END as value,
                                  created_at, updated_at
                           FROM secrets WHERE scope_type = 'env' AND scope_id = ? ORDER BY type, name""",
                        (env_id,)
                    )
                    for row in await cursor.fetchall():
                        d = dict(row)
                        d["inherited"] = True
                        d["inherited_from"] = "env"
                        key = (d["name"], d["type"])
                        if key in seen_names:
                            results[seen_names[key]] = d  # Override
                        else:
                            seen_names[key] = len(results)
                            results.append(d)

        elif scope_type == "env" and include_inherited:
            # Get org-level secrets for inheritance
            cursor = await db.execute(
                "SELECT org_id FROM environments WHERE id = ?", (scope_id,)
            )
            env_row = await cursor.fetchone()
            if env_row and env_row["org_id"]:
                cursor = await db.execute(
                    """SELECT id, scope_type, scope_id, name, type, description,
                              CASE WHEN type = 'secret' THEN '••••••••' ELSE value_encrypted END as value,
                              created_at, updated_at
                       FROM secrets WHERE scope_type = 'org' AND scope_id = ? ORDER BY type, name""",
                    (env_row["org_id"],)
                )
                for row in await cursor.fetchall():
                    d = dict(row)
                    d["inherited"] = True
                    d["inherited_from"] = "org"
                    key = (d["name"], d["type"])
                    seen_names[key] = len(results)
                    results.append(d)

        # Get secrets at the requested scope (highest priority)
        cursor = await db.execute(
            """SELECT id, scope_type, scope_id, name, type, description,
                      CASE WHEN type = 'secret' THEN '••••••••' ELSE value_encrypted END as value,
                      created_at, updated_at
               FROM secrets WHERE scope_type = ? AND scope_id = ? ORDER BY type, name""",
            (scope_type, scope_id)
        )
        for row in await cursor.fetchall():
            d = dict(row)
            d["inherited"] = False
            d["inherited_from"] = None
            key = (d["name"], d["type"])
            if key in seen_names:
                results[seen_names[key]] = d  # Override inherited
            else:
                seen_names[key] = len(results)
                results.append(d)

        return {"secrets": results}
    finally:
        await db.close()


@router.post("")
async def create_secret(secret: SecretCreate):
    db = await get_db()
    try:
        # Verify scope exists
        if secret.scope_type == "workspace":
            cursor = await db.execute("SELECT id FROM workspaces WHERE id = ?", (secret.scope_id,))
        elif secret.scope_type == "env":
            cursor = await db.execute("SELECT id FROM environments WHERE id = ?", (secret.scope_id,))
        elif secret.scope_type == "org":
            cursor = await db.execute("SELECT id FROM organizations WHERE id = ?", (secret.scope_id,))
        else:
            raise HTTPException(status_code=400, detail="Invalid scope_type. Must be org, env, or workspace")

        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"{secret.scope_type} not found")

        # Upsert: if a secret with the same name already exists at this scope,
        # update it instead of raising an error.
        cursor = await db.execute(
            "SELECT id FROM secrets WHERE scope_type = ? AND scope_id = ? AND name = ?",
            (secret.scope_type, secret.scope_id, secret.name)
        )
        existing = await cursor.fetchone()
        now = datetime.utcnow().isoformat()

        if existing:
            existing_id = existing["id"]
            await db.execute(
                "UPDATE secrets SET value_encrypted = ?, type = ?, description = ?, updated_at = ? WHERE id = ?",
                (secret.value, secret.type, secret.description or "", now, existing_id)
            )
            await db.commit()
            return {"id": existing_id, "name": secret.name, "type": secret.type, "scope_type": secret.scope_type, "updated": True}

        secret_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO secrets (id, scope_type, scope_id, name, value_encrypted, type, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (secret_id, secret.scope_type, secret.scope_id, secret.name, secret.value, secret.type, secret.description, now, now)
        )
        await db.commit()
        return {"id": secret_id, "name": secret.name, "type": secret.type, "scope_type": secret.scope_type}
    finally:
        await db.close()


@router.put("/{secret_id}")
async def update_secret(secret_id: str, update: SecretUpdate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM secrets WHERE id = ?", (secret_id,))
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Secret not found")

        _ALLOWED_FIELDS = {"name", "value_encrypted", "description"}
        field_values = {}
        if update.name is not None:
            field_values["name"] = update.name
        if update.value is not None:
            field_values["value_encrypted"] = update.value
        if update.description is not None:
            field_values["description"] = update.description

        if field_values:
            field_values["updated_at"] = datetime.utcnow().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in field_values)
            params = list(field_values.values()) + [secret_id]
            await db.execute(f"UPDATE secrets SET {set_clause} WHERE id = ?", params)
            await db.commit()

        return {"status": "updated"}
    finally:
        await db.close()


@router.delete("/{secret_id}")
async def delete_secret(secret_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM secrets WHERE id = ?", (secret_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Secret not found")

        await db.execute("DELETE FROM secrets WHERE id = ?", (secret_id,))
        await db.commit()
        return {"status": "deleted"}
    finally:
        await db.close()
