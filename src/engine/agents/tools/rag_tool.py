"""
RAG Tool

LangChain tool for document search via RAG collection.
"""
import logging
from langchain_core.tools import tool

from langchain_postgres import PGVector
from langchain_ollama.embeddings import OllamaEmbeddings

from ...config import Config

logger = logging.getLogger("rugpt.agents.tools.rag")


@tool(response_format='content')
def rag_search(query: str, collection: str = "") -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query
        collection: Optional RAG collection name to search in
    """
    emb = OllamaEmbeddings(model='nomic-embed-text-v2-moe:latest', base_url='http://localhost:11434')
    client = PGVector(connection=Config.get_vector_dsn(),embeddings=emb,collection_name=collection)
    
    logger.info(f"rag_search called: query={query}, collection={collection}")
    
    docs = client.similarity_search(query, k = 6)
    scored_docs = []

    keywords = query.split()
    for doc in docs:
        score = 0
        for keyword in keywords:
            score += 1 if keyword in doc.page_content else 0
        scored_docs.append((score, doc))
    
    logging.info("Scored:", [score for score, doc in scored_docs])
    scored_docs.sort(key = lambda x: x[0], reverse=True)
    top_docs = scored_docs[0:3]

    logging.info("Top chosen from hybrid search:", [score for score, doc in top_docs])

    serialized = "\n\n".join((f"Score: {score}\nDocument: {doc.page_content}\nMetadata: {doc.metadata}\n") for score, doc in top_docs)
    return serialized
