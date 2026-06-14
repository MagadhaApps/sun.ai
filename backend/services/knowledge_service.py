import json
import litellm
from database import get_db, CURRENT_DATABASE

async def search_knowledge(kb_ids: list, query: str, limit: int = 5) -> str:
    if not kb_ids or not query:
        return "No knowledge base connected or empty query."
        
    if CURRENT_DATABASE != "postgres":
        return "Knowledge search is only supported when running on PostgreSQL with pgvector."

    db = await get_db()
    try:
        # Generate embedding for the search query
        response = litellm.embedding(model="text-embedding-3-small", input=[query])
        query_embedding = response.data[0]["embedding"]
        embedding_val = f"[{','.join(map(str, query_embedding))}]"
        
        # Validate kb_ids before building query
        if not kb_ids or not isinstance(kb_ids, list):
            return "No knowledge bases specified."
        # Ensure all kb_ids are valid strings to prevent injection
        import re
        _id_pat = re.compile(r'^[a-zA-Z0-9_\-]+$')
        for kid in kb_ids:
            if not isinstance(kid, str) or not _id_pat.match(kid):
                return f"Invalid knowledge base ID: {kid}"

        # Build IN clause placeholders safely — only "?" markers, no values interpolated
        placeholders = ", ".join("?" for _ in kb_ids)
        
        # Perform cosine similarity search (<=>)
        # All values are passed as parameters; placeholders contain only "?" markers
        sql = (
            "SELECT title, content, 1 - (embedding <=> ?::vector) as similarity "
            "FROM knowledge_documents "
            "WHERE kb_id IN ({placeholders}) "
            "ORDER BY embedding <=> ?::vector "
            "LIMIT ?"
        ).format(placeholders=placeholders)
        params = [embedding_val] + kb_ids + [embedding_val, limit]
        
        cursor = await db.execute(sql, tuple(params))
        rows = await cursor.fetchall()
        
        if not rows:
            return "No relevant information found in the knowledge base."
            
        results = []
        for r in rows:
            results.append(f"Title: {r['title']}\nContent:\n{r['content']}\n")
            
        return "\n---\n".join(results)
    except Exception as e:
        print(f"Error during knowledge search: {e}")
        return f"Error executing search: {str(e)}"
    finally:
        await db.close()
