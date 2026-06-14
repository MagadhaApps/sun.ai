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
        
        # Build IN clause for kb_ids
        placeholders = ", ".join("?" for _ in kb_ids)
        params = [embedding_val] + kb_ids + [limit]
        
        # Perform cosine similarity search (<=>)
        # Assuming embedding vector(1536)
        sql = (
            "SELECT title, content, 1 - (embedding <=> ?::vector) as similarity "
            "FROM knowledge_documents "
            "WHERE kb_id IN (" + placeholders + ") "
            "ORDER BY embedding <=> ?::vector "
            "LIMIT ?"
        )
        # Wait, the parameter binding for vector distance in Databases requires slightly careful typing
        # Let's adjust query to use explicit cast if needed, or rely on execution parsing.
        # Actually Databases parameters are named, execute_query translates "?" to positional.
        # However, the embedding in ORDER BY needs the same parameter.
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
