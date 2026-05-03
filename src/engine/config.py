"""
RuGPT Engine Configuration

Configuration class for the RuGPT corporate AI assistant engine.
"""
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from uuid import UUID
from dotenv import load_dotenv

# Load .env file from project root
_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path)


class Config:
    """Configuration class for RuGPT Engine API"""

    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    # Base paths
    BASE_DIR = Path(__file__).parent.parent.parent
    DATA_DIR = BASE_DIR / "data"

    # Database settings
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME", "rugpt")
    DB_USER = os.getenv("DB_USER", "postgres")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    # PostgreSQL DSN (use get_postgres_dsn() method for proper password escaping)
    _db_password_escaped = quote_plus(DB_PASSWORD) if DB_PASSWORD else ""
    POSTGRES_DSN = os.getenv(
        "POSTGRES_DSN",
        f"postgresql://{DB_USER}:{_db_password_escaped}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        if DB_PASSWORD else f"postgresql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    # Redis settings
    REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
    REDIS_PORT = os.getenv("REDIS_PORT", "6379")
    REDIS_DB = os.getenv("REDIS_DB", "0")
    REDIS_URL = os.getenv("REDIS_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}")

    # Kafka settings (item 10: PM-agent + async inference via agent.requests / chat.events)
    KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    KAFKA_ENABLED = os.getenv("KAFKA_ENABLED", "true").lower() == "true"
    KAFKA_TOPIC_AGENT_REQUESTS = os.getenv("KAFKA_TOPIC_AGENT_REQUESTS", "agent.requests")
    KAFKA_TOPIC_CHAT_EVENTS = os.getenv("KAFKA_TOPIC_CHAT_EVENTS", "chat.events")
    KAFKA_CONSUMER_GROUP_AGENT_RUNNERS = os.getenv(
        "KAFKA_CONSUMER_GROUP_AGENT_RUNNERS", "engine-agent-runners"
    )

    # Session TTL (seconds)
    SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))

    # ---- Support chat (Tech Support feature) ----
    # RuGPT Support organization UUID — hard-coded in migration 023.
    RUGPT_SUPPORT_ORG_ID = UUID("00000001-0000-0000-0000-000000000000")
    # System organization (RuGPT) — holds AI system users (support_ai, pm, etc.)
    # Hard-coded in migrations 003 and 023.
    SYSTEM_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")
    # Reopen window for closed support tickets via message in chat.
    SUPPORT_REOPEN_WINDOW_DAYS = int(os.getenv("SUPPORT_REOPEN_WINDOW_DAYS", "7"))
    # Username of the AI first-line support system user (created by migration 023).
    SUPPORT_AI_USERNAME = "support_ai"

    # Task query tool limits. Raise TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET when the
    # model supports a larger context window; replace with a token budget once a
    # token-counting service is available (see TODO in task_tool.py).
    TASKS_QUERY_LIMIT = int(os.getenv("TASKS_QUERY_LIMIT", "200"))
    TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET = int(os.getenv("TASKS_QUERY_DESCRIPTIONS_CHAR_BUDGET", "20000"))

    # API settings
    API_HOST = os.getenv("API_HOST", "127.0.0.1")
    API_PORT = int(os.getenv("API_PORT", "8100"))

    # LLM settings — all inference (generation + embeddings) goes through a
    # single OpenAI-compatible gateway (LiteLLM proxy on Zver). LiteLLM itself
    # fans out to Ollama / vLLM behind the scenes based on model name.
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://192.168.1.80:4000/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-dummy")
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "google/gemma-4-31B-it")
    IMAGE_ANALYSIS_MODEL = os.getenv("IMAGE_ANALYSIS_MODEL", DEFAULT_MODEL)

    # Legacy OpenAI fields kept as aliases — some older code paths may still
    # read them, but new code should use LLM_* above.
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", LLM_API_KEY)
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Perplexity API (web_search tool)
    PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY", "")

    # JWT settings
    JWT_SECRET = os.getenv("JWT_SECRET", "rugpt-secret-key-change-in-production")
    JWT_ALGORITHM = "HS256"
    JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", "24"))

    # Password hashing
    PASSWORD_SALT_ROUNDS = int(os.getenv("PASSWORD_SALT_ROUNDS", "12"))

    # Scheduler settings
    SCHEDULER_POLL_INTERVAL = os.getenv("SCHEDULER_POLL_INTERVAL", "30")
    SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() == "true"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # SMTP (Email notifications)
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

    # File storage
    STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")  # local | s3
    STORAGE_LOCAL_DIR = os.getenv("STORAGE_LOCAL_DIR", str(Path(__file__).parent.parent.parent / "uploads"))
    FILE_MAX_SIZE_MB = int(os.getenv("FILE_MAX_SIZE_MB", "50"))
    FILE_ALLOWED_TYPES = os.getenv("FILE_ALLOWED_TYPES", "pdf,docx")

    # RAG / Embeddings
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    RAG_SUMMARY_MODEL = os.getenv("RAG_SUMMARY_MODEL", DEFAULT_MODEL)
    RAG_TIKA_SERVER_ENDPOINT = os.getenv("RAG_TIKA_SERVER_ENDPOINT", "http://localhost:9998")
    RAG_STORE_DSN = os.getenv("RAG_STORE_DSN", POSTGRES_DSN)
    RAG_VECTOR_DIM = int(os.getenv("RAG_VECTOR_DIM", "1024"))
    RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "1000"))
    RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "200"))
    RAG_SUMMARY_INPUT_MAX_CHARS = int(os.getenv("RAG_SUMMARY_INPUT_MAX_CHARS", "15000"))

    @staticmethod
    def get_postgres_dsn() -> str:
        """Get PostgreSQL DSN with password handling"""
        if Config.DB_PASSWORD:
            password = quote_plus(Config.DB_PASSWORD)
            return f"postgresql://{Config.DB_USER}:{password}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
        return f"postgresql://{Config.DB_USER}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"

    @staticmethod
    def get_vector_dsn() -> str:
        """Get PostgreSQL DSN for pgvector (psycopg2/psycopg3 format)"""
        if Config.DB_PASSWORD:
            password = quote_plus(Config.DB_PASSWORD)
            return f"postgresql+psycopg://{Config.DB_USER}:{password}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
        return f"postgresql+psycopg://{Config.DB_USER}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
