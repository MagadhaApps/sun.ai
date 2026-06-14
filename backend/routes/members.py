from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime
from database import get_db
import uuid
import os
import resend
from dotenv import load_dotenv

load_dotenv()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

router = APIRouter()

# ─── Pydantic Models ───

class MemberInvite(BaseModel):
    user_email: EmailStr
    role: str = "member" # owner, admin, member, viewer

class MemberUpdate(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None

# ─── API Routes ───

@router.get("")
async def list_members(org_id: str, x_user_email: str = Header(...)):
    db = await get_db()
    try:
        # Validate requestor has access to view members
        cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, x_user_email))
        requestor = await cursor.fetchone()
        if not requestor:
            raise HTTPException(status_code=403, detail="Not authorized to view members for this organization")

        cursor = await db.execute("SELECT * FROM organization_members WHERE org_id = ? ORDER BY created_at DESC", (org_id,))
        rows = await cursor.fetchall()
        return {"members": [dict(r) for r in rows]}
    finally:
        await db.close()

@router.post("")
async def invite_member(org_id: str, invite: MemberInvite, x_user_email: str = Header(...)):
    db = await get_db()
    try:
        # Validate requestor is owner or admin
        cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, x_user_email))
        requestor = await cursor.fetchone()
        if not requestor or requestor['role'] not in ['owner', 'admin']:
            raise HTTPException(status_code=403, detail="Not authorized to invite members")

        if invite.role not in ['owner', 'admin', 'member', 'viewer']:
            raise HTTPException(status_code=400, detail="Invalid role")

        # Check if user already exists
        cursor = await db.execute("SELECT id FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, invite.user_email))
        if await cursor.fetchone():
            raise HTTPException(status_code=400, detail="User is already a member or has a pending invite")

        member_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        await db.execute(
            "INSERT INTO organization_members (id, org_id, user_email, role, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, org_id, invite.user_email, invite.role, "pending", now, now)
        )
        await db.commit()

        # Get org name for email
        cursor = await db.execute("SELECT name FROM organizations WHERE id = ?", (org_id,))
        org_row = await cursor.fetchone()
        org_name = org_row['name'] if org_row else "an organization"

        if RESEND_API_KEY:
            try:
                resend.Emails.send({
                    "from": "Zeus.ai <onboarding@vividai.tech>",
                    "to": invite.user_email,
                    "subject": f"You've been invited to join {org_name} on Zeus.ai",
                    "html": f"""
                    <h2>You've been invited to a workspace!</h2>
                    <p><strong>{x_user_email}</strong> has invited you to join <strong>{org_name}</strong> as a <strong>{invite.role}</strong>.</p>
                    <p><a href="http://localhost:3000/orgs/members">Click here to log in and accept your invitation</a>.</p>
                    <p>Welcome aboard,</p>
                    <p>The Zeus.ai Team</p>
                    """
                })
            except Exception as e:
                print(f"Failed to send email via Resend: {e}")

        return {"id": member_id, "user_email": invite.user_email, "role": invite.role, "status": "pending"}
    finally:
        await db.close()

@router.put("/{user_email}")
async def update_member(org_id: str, user_email: str, update: MemberUpdate, x_user_email: str = Header(...)):
    db = await get_db()
    try:
        now = datetime.utcnow().isoformat()
        
        # Self-acceptance of invites
        if user_email == x_user_email and update.status == "active" and not update.role:
            cursor = await db.execute("SELECT status FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, user_email))
            member = await cursor.fetchone()
            if not member or member['status'] != 'pending':
                raise HTTPException(status_code=400, detail="No pending invite found")
                
            await db.execute("UPDATE organization_members SET status = ?, updated_at = ? WHERE org_id = ? AND user_email = ?", ('active', now, org_id, user_email))
            await db.commit()
            return {"status": "success"}

        # Otherwise, need admin/owner permissions to update roles
        cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, x_user_email))
        requestor = await cursor.fetchone()
        if not requestor or requestor['role'] not in ['owner', 'admin']:
            raise HTTPException(status_code=403, detail="Not authorized to modify members")
            
        # Prevent demoting the last owner
        if update.role and update.role != 'owner':
            cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, user_email))
            target = await cursor.fetchone()
            if target and target['role'] == 'owner':
                cursor = await db.execute("SELECT COUNT(*) as count FROM organization_members WHERE org_id = ? AND role = 'owner'", (org_id,))
                owner_count = await cursor.fetchone()
                if owner_count['count'] <= 1:
                    raise HTTPException(status_code=400, detail="Cannot demote the last owner of the organization")

        if update.role:
            if update.role not in ['owner', 'admin', 'member', 'viewer']:
                raise HTTPException(status_code=400, detail="Invalid role")
        if update.status and update.status not in ['active', 'pending', 'inactive']:
            raise HTTPException(status_code=400, detail="Invalid status")
            
        if not update.role and not update.status:
            return {"status": "success"}

        # Fetch the current member row to preserve unchanged fields
        cursor = await db.execute(
            "SELECT * FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, user_email)
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Member not found")
            
        await db.execute(
            "UPDATE organization_members SET role = ?, status = ?, updated_at = ? WHERE org_id = ? AND user_email = ?",
            (
                update.role if update.role else existing["role"],
                update.status if update.status else existing["status"],
                now,
                org_id,
                user_email,
            ),
        )
        await db.commit()
        return {"status": "success"}
    finally:
        await db.close()

@router.delete("/{user_email}")
async def remove_member(org_id: str, user_email: str, x_user_email: str = Header(...)):
    db = await get_db()
    try:
        # Check if user is removing themselves (leaving) or if requestor is admin/owner
        if user_email != x_user_email:
            cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, x_user_email))
            requestor = await cursor.fetchone()
            if not requestor or requestor['role'] not in ['owner', 'admin']:
                raise HTTPException(status_code=403, detail="Not authorized to remove members")
                
        # Prevent removing the last owner
        cursor = await db.execute("SELECT role FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, user_email))
        target = await cursor.fetchone()
        if target and target['role'] == 'owner':
            cursor = await db.execute("SELECT COUNT(*) as count FROM organization_members WHERE org_id = ? AND role = 'owner'", (org_id,))
            owner_count = await cursor.fetchone()
            if owner_count['count'] <= 1:
                raise HTTPException(status_code=400, detail="Cannot remove the last owner of the organization")

        await db.execute("DELETE FROM organization_members WHERE org_id = ? AND user_email = ?", (org_id, user_email))
        await db.commit()
        return {"status": "success"}
    finally:
        await db.close()
