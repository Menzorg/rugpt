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

RUSSIAN_STOP_WORDS = [
    # pronouns
    "я", "ты", "он", "она", "оно", "мы", "вы", "они",
    "меня", "тебя", "его", "ее", "её", "нас", "вас", "их",
    "мне", "тебе", "ему", "ей", "нам", "вам", "ими",
    "мой", "моя", "мое", "моё", "мои",
    "твой", "твоя", "твое", "твои",
    "наш", "наша", "наше", "наши",
    "ваш", "ваша", "ваше", "ваши",
    "свой", "своя", "свое", "свои",

    # prepositions
    "в", "во", "на", "к", "ко", "с", "со", "от", "до", "по",
    "за", "из", "у", "о", "об", "обо", "при",
    "про", "для", "через", "под", "над", "между",

    # conjunctions
    "и", "а", "но", "или", "либо", "да", "что", "чтобы",
    "как", "если", "то", "же", "так", "потому", "поскольку",

    # particles
    "бы", "б", "ли", "же", "вот", "вот", "ну",
    "уж", "еще", "ещё", "уже", "лишь", "только",

    # question words
    "кто", "что", "где", "когда", "куда", "откуда",
    "почему", "зачем", "какой", "какая", "какие", "какое",

    # common verbs (weak semantic weight)
    "есть", "быть", "был", "была", "были",
    "может", "мог", "могут", "нужно", "надо",
    "является", "являются", "имеет", "имеют",

    # common adverbs
    "там", "тут", "здесь", "также", "такой",
    "очень", "сейчас", "тогда",

    # fillers
    "это", "этот", "эта", "эти", "того", "тому",
    "та", "те", "тот", "такой", "такая",
]

@tool(response_format='content')
def rag_search(query: str, collection: str = "") -> str:
    """Search documents in the knowledge base.
    Args:
        query: Search query
        collection: Optional RAG collection name to search in
    """
    emb = OllamaEmbeddings(model='nomic-embed-text-v2-moe:latest', base_url='http://localhost:11434')
    client = PGVector(connection=Config.get_vector_dsn(),embeddings=emb,collection_name="law")
    
    logger.info(f"rag_search called: query={query} collection={collection}")
    
    docs = client.similarity_search(query, k = 5)
    scored_docs = []

    keywords = set(query.split()) - set(RUSSIAN_STOP_WORDS)
    for doc in docs:
        score = 0
        for keyword in keywords:
            score += 1 if keyword in doc.page_content else 0
        scored_docs.append((score, doc))
    
    logger.info(f"Scored: {[score for score, doc in scored_docs]}")
    scored_docs.sort(key = lambda x: x[0], reverse=True)
    top_docs = scored_docs[0:2]

    logger.info(f"Top chosen from hybrid search: {[score for score, doc in top_docs]}")
    if (len(top_docs) > 0):
        logger.info(f"Best result from RAG: {top_docs[0][1].page_content}")

        return "\n\n".join((f"Score: {score}\nDocument: {doc.page_content}\nMetadata: {doc.metadata}\n") for score, doc in top_docs)
    else:
        logger.info(f"Nothing found")
        return "Nothing found"

