"""Community-driven RAG knowledge base using ChromaDB + Gemini embeddings.

Stores highly-upvoted Discord messages (from thread answers) as vector embeddings.
Retrieves the most relevant community explanations for a given set of topics.

ChromaDB runs entirely locally — zero cloud setup, zero cost.
"""

from __future__ import annotations

import os
import chromadb
from google import genai
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# Gemini Embedding
# ─────────────────────────────────────────

_genai_client = genai.Client()
EMBED_MODEL = "gemini-embedding-001"


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Get embeddings for a list of texts using Gemini's embedding model."""
    results = []
    for text in texts:
        resp = _genai_client.models.embed_content(
            model=EMBED_MODEL,
            contents=text,
        )
        results.append(resp.embeddings[0].values)
    return results


# ─────────────────────────────────────────
# ChromaDB Setup
# ─────────────────────────────────────────

DB_DIR = os.path.join(os.path.dirname(__file__), "chromadb_data")

_chroma_client = chromadb.PersistentClient(path=DB_DIR)


def _get_collection(guild_id: int) -> chromadb.Collection:
    """Get or create a ChromaDB collection for a specific guild."""
    name = f"guild_{guild_id}"
    return _chroma_client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


# ─────────────────────────────────────────
# Public API
# ─────────────────────────────────────────

def add_message(
    guild_id: int,
    message_id: int,
    author_name: str,
    content: str,
    channel_name: str,
    thread_name: str = "",
    reaction_count: int = 1,
) -> None:
    """Add a highly-upvoted message to the RAG knowledge base."""
    collection = _get_collection(guild_id)

    doc_id = str(message_id)

    # Check if already stored (avoid duplicates)
    existing = collection.get(ids=[doc_id])
    if existing and existing["ids"]:
        # Update reaction count in metadata
        collection.update(
            ids=[doc_id],
            metadatas=[{
                "author": author_name,
                "channel": channel_name,
                "thread": thread_name,
                "reactions": reaction_count,
            }],
        )
        print(f"  📝 Updated RAG entry {doc_id} (reactions: {reaction_count})")
        return

    # Generate embedding
    embedding = _embed_texts([content])[0]

    collection.add(
        ids=[doc_id],
        documents=[content],
        embeddings=[embedding],
        metadatas=[{
            "author": author_name,
            "channel": channel_name,
            "thread": thread_name,
            "reactions": reaction_count,
        }],
    )
    print(f"  ✅ Added to RAG: [{author_name}] {content[:80]}...")


def query_knowledge(guild_id: int, topics: list[str], top_k: int = 5) -> str:
    """Query the knowledge base for the most relevant community explanations.

    Returns a formatted string ready to inject into the Gemini prompt.
    """
    collection = _get_collection(guild_id)

    if collection.count() == 0:
        return ""

    # Combine topics into a single query
    query_text = " ".join(topics)
    query_embedding = _embed_texts([query_text])[0]

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
    )

    if not results or not results["documents"] or not results["documents"][0]:
        return ""

    # Format results for the prompt
    lines = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        author = meta.get("author", "Unknown")
        reactions = meta.get("reactions", 0)
        lines.append(f'[Author: @{author} | 👍 {reactions}] "{doc}"')

    formatted = "\n".join(lines)
    print(f"  🧠 RAG: Retrieved {len(lines)} community explanations")
    return formatted


def get_stats(guild_id: int) -> dict:
    """Get stats about the knowledge base for a guild."""
    collection = _get_collection(guild_id)
    count = collection.count()
    return {"total_entries": count}


def remove_message(guild_id: int, message_id: int) -> bool:
    """Remove a message from the knowledge base (e.g., if reactions drop below threshold)."""
    collection = _get_collection(guild_id)
    doc_id = str(message_id)
    try:
        existing = collection.get(ids=[doc_id])
        if existing and existing["ids"]:
            collection.delete(ids=[doc_id])
            return True
    except Exception:
        pass
    return False


def add_document_chunks(
    guild_id: int,
    author_name: str,
    content: str,
    source_name: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> int:
    """Split a large document (e.g. PDF text) into chunks and add each to the RAG.

    Returns the number of chunks added.
    """
    collection = _get_collection(guild_id)

    # Simple chunking by character count with overlap
    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = start + chunk_size
        chunk = content[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - chunk_overlap

    if not chunks:
        return 0

    added = 0
    for i, chunk in enumerate(chunks):
        # Use a deterministic ID based on source + chunk index so re-uploads update
        doc_id = f"doc_{guild_id}_{source_name}_{i}"

        embedding = _embed_texts([chunk])[0]

        # Upsert: delete existing then add
        try:
            existing = collection.get(ids=[doc_id])
            if existing and existing["ids"]:
                collection.delete(ids=[doc_id])
        except Exception:
            pass

        collection.add(
            ids=[doc_id],
            documents=[chunk],
            embeddings=[embedding],
            metadatas=[{
                "author": author_name,
                "channel": "pdf-upload",
                "thread": source_name,
                "reactions": 0,
                "source_type": "pdf",
                "chunk_index": i,
                "total_chunks": len(chunks),
            }],
        )
        added += 1

    print(f"  ✅ Added {added} chunks from '{source_name}' to RAG for guild {guild_id}")
    return added
