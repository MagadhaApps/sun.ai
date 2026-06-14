import json
import uuid
from datetime import datetime, timedelta
from database import get_db
from services.provider_service import MODEL_PRICING


async def log_llm_call(
    provider_id: str = None,
    provider_name: str = "",
    model_id: str = "",
    model_name: str = "",
    input_text: str = "",
    output_text: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    cost: float = 0,
    latency_ms: int = 0,
    ttfb_ms: int = 0,
    status: str = "success",
    error: str = None,
    source: str = "chat",
    conversation_id: str = None,
    workflow_id: str = None,
    workflow_execution_id: str = None,
    metadata: dict = None,
    org_id: str = None,
    workspace_id: str = None,
):
    # Calculate cost based on model pricing
    if cost == 0 and model_id:
        pricing = MODEL_PRICING.get(model_id, {})
        input_cost = (input_tokens / 1000) * pricing.get("input", 0)
        output_cost = (output_tokens / 1000) * pricing.get("output", 0)
        cost = round(input_cost + output_cost, 8)

    total_tokens = input_tokens + output_tokens

    log_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO observability_logs
               (id, org_id, workspace_id, type, source, provider_id, provider_name, model_id, model_name,
                input_text, output_text, input_tokens, output_tokens, cached_tokens,
                total_tokens, cost, latency_ms, ttfb_ms, status, error, metadata,
                conversation_id, workflow_id, workflow_execution_id, created_at)
               VALUES (?, ?, ?, 'llm_call', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (log_id, org_id, workspace_id, source, provider_id, provider_name, model_id, model_name,
             input_text[:10000], output_text[:10000], input_tokens, output_tokens, cached_tokens,
             total_tokens, cost, latency_ms, ttfb_ms, status, error,
             json.dumps(metadata or {}), conversation_id, workflow_id, workflow_execution_id, now)
        )
        await db.commit()
    finally:
        await db.close()

    return log_id


async def get_logs(
    limit: int = 50,
    offset: int = 0,
    source: str = None,
    provider_id: str = None,
    model_id: str = None,
    status: str = None,
    start_date: str = None,
    end_date: str = None,
    org_id: str = None,
    workspace_id: str = None,
):
    db = await get_db()
    try:
        # Build WHERE clause from allowed condition templates
        # Each condition template maps a parameter to its SQL fragment; all fragments
        # use ? placeholders and are validated against a known set of column names.
        _ALLOWED_COLUMNS = {"org_id", "workspace_id", "source", "provider_id",
                            "model_id", "status", "created_at"}
        conditions = []
        params = []

        def _add_condition(column: str, op: str, value):
            if column not in _ALLOWED_COLUMNS:
                raise ValueError(f"Invalid filter column: {column}")
            conditions.append(f"{column} {op} ?")
            params.append(value)

        if org_id:
            _add_condition("org_id", "=", org_id)
        if workspace_id:
            _add_condition("workspace_id", "=", workspace_id)
        if source:
            _add_condition("source", "=", source)
        if provider_id:
            _add_condition("provider_id", "=", provider_id)
        if model_id:
            _add_condition("model_id", "=", model_id)
        if status:
            _add_condition("status", "=", status)
        if start_date:
            _add_condition("created_at", ">=", start_date)
        if end_date:
            _add_condition("created_at", "<=", end_date)

        where = " AND ".join(conditions) if conditions else "1=1"

        count_cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM observability_logs WHERE {where}".format(where=where), params
        )
        count_row = await count_cursor.fetchone()
        total = count_row["cnt"] if count_row else 0

        cursor = await db.execute(
            "SELECT id, type, source, provider_name, model_name, input_tokens, output_tokens, "
            "cached_tokens, total_tokens, cost, latency_ms, ttfb_ms, status, error, "
            "org_id, workspace_id, created_at "
            "FROM observability_logs WHERE {where} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?".format(where=where),
            params + [limit, offset]
        )
        rows = await cursor.fetchall()
        logs = [dict(r) for r in rows]

        return {"logs": logs, "total": total, "limit": limit, "offset": offset}
    finally:
        await db.close()


async def get_log_detail(log_id: str):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM observability_logs WHERE id = ?", (log_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        log = dict(row)
        log["metadata"] = json.loads(log.get("metadata", "{}"))
        return log
    finally:
        await db.close()


async def get_stats(start_date: str = None, end_date: str = None, org_id: str = None, workspace_id: str = None):
    db = await get_db()
    try:
        conditions = []
        params = []
        _ALLOWED_COLUMNS = {"org_id", "workspace_id", "created_at"}
        def _add_cond(col, op, val):
            if col not in _ALLOWED_COLUMNS:
                raise ValueError(f"Invalid filter column: {col}")
            conditions.append(f"{col} {op} ?")
            params.append(val)
        if org_id:
            _add_cond("org_id", "=", org_id)
        if workspace_id:
            _add_cond("workspace_id", "=", workspace_id)
        if start_date:
            _add_cond("created_at", ">=", start_date)
        if end_date:
            _add_cond("created_at", "<=", end_date)
        where = " AND ".join(conditions) if conditions else "1=1"

        cursor = await db.execute(
            "SELECT "
            "COUNT(*) as total_requests, "
            "SUM(input_tokens) as total_input_tokens, "
            "SUM(output_tokens) as total_output_tokens, "
            "SUM(cached_tokens) as total_cached_tokens, "
            "SUM(total_tokens) as total_tokens, "
            "SUM(cost) as total_cost, "
            "AVG(latency_ms) as avg_latency_ms, "
            "AVG(ttfb_ms) as avg_ttfb_ms, "
            "MIN(latency_ms) as min_latency_ms, "
            "MAX(latency_ms) as max_latency_ms, "
            "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count, "
            "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count "
            "FROM observability_logs WHERE {where}".format(where=where),
            params
        )
        row = await cursor.fetchone()
        stats = dict(row) if row else {}

        # Per-model stats
        model_cursor = await db.execute(
            "SELECT model_name, "
            "COUNT(*) as requests, "
            "SUM(total_tokens) as tokens, "
            "SUM(cost) as cost, "
            "AVG(latency_ms) as avg_latency "
            "FROM observability_logs WHERE {where} "
            "GROUP BY model_name ORDER BY requests DESC LIMIT 20".format(where=where),
            params
        )
        model_rows = await model_cursor.fetchall()
        stats["by_model"] = [dict(r) for r in model_rows]

        # Per-provider stats
        prov_cursor = await db.execute(
            "SELECT provider_name, "
            "COUNT(*) as requests, "
            "SUM(total_tokens) as tokens, "
            "SUM(cost) as cost, "
            "AVG(latency_ms) as avg_latency "
            "FROM observability_logs WHERE {where} "
            "GROUP BY provider_name ORDER BY requests DESC".format(where=where),
            params
        )
        prov_rows = await prov_cursor.fetchall()
        stats["by_provider"] = [dict(r) for r in prov_rows]

        return stats
    finally:
        await db.close()


async def get_timeseries(interval: str = "hour", start_date: str = None, end_date: str = None,
                         org_id: str = None, workspace_id: str = None):
    db = await get_db()
    try:
        # Validate interval against allowed values and map to safe SQL expressions
        _ALLOWED_INTERVALS = {
            "hour": "strftime('%Y-%m-%dT%H:00:00', created_at)",
            "day": "strftime('%Y-%m-%d', created_at)",
        }
        group_expr = _ALLOWED_INTERVALS.get(interval, _ALLOWED_INTERVALS["hour"])

        conditions = []
        params = []
        _ALLOWED_COLUMNS = {"org_id", "workspace_id", "created_at"}
        def _add_cond(col, op, val):
            if col not in _ALLOWED_COLUMNS:
                raise ValueError(f"Invalid filter column: {col}")
            conditions.append(f"{col} {op} ?")
            params.append(val)
        if org_id:
            _add_cond("org_id", "=", org_id)
        if workspace_id:
            _add_cond("workspace_id", "=", workspace_id)
        if start_date:
            _add_cond("created_at", ">=", start_date)
        if end_date:
            _add_cond("created_at", "<=", end_date)
        where = " AND ".join(conditions) if conditions else "1=1"

        cursor = await db.execute(
            "SELECT {group_expr} as time_bucket, "
            "COUNT(*) as requests, "
            "SUM(total_tokens) as tokens, "
            "SUM(cost) as cost, "
            "AVG(latency_ms) as avg_latency, "
            "SUM(input_tokens) as input_tokens, "
            "SUM(output_tokens) as output_tokens "
            "FROM observability_logs WHERE {where} "
            "GROUP BY time_bucket ORDER BY time_bucket".format(group_expr=group_expr, where=where),
            params
        )
        rows = await cursor.fetchall()
        return {"data": [dict(r) for r in rows], "interval": interval}
    finally:
        await db.close()
