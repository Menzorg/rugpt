"""
RuGPT Engine Configuration

Configuration class for the RuGPT corporate AI assistant engine.
"""
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus
from dotenv import load_dotenv

# Load .env file from project root
_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path)


class Config:
    """Configuration class for RuGPT Engine API"""

    # Base paths
    BASE_DIR = Path(__file__).parent.parent.parent
    DATA_DIR = BASE_DIR / "data"

    # Database settings
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = os.getenv("DB_PORT", "5432")
    DB_NAME = os.getenv("DB_NAME", "rugpt")
    DB_USER = os.getenv("DB_USER", "postgres")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")

    VECTOR_PORT = os.getenv("VECTOR_PORT", "7432")

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

    # Session TTL (seconds)
    SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))

    # API settings
    API_HOST = os.getenv("API_HOST", "127.0.0.1")
    API_PORT = int(os.getenv("API_PORT", "8100"))

    # LLM settings
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434")  # Ollama default
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2:0.5b")

    # OpenAI fallback (optional)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

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

    @staticmethod
    def get_postgres_dsn() -> str:
        """Get PostgreSQL DSN with password handling"""
        if Config.DB_PASSWORD:
            password = quote_plus(Config.DB_PASSWORD)
            return f"postgresql://{Config.DB_USER}:{password}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"
        return f"postgresql://{Config.DB_USER}@{Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}"

    @staticmethod
    def get_vector_dsn() -> str:
        """Get PostgreSQL DSN with password handling"""
        if Config.DB_PASSWORD:
            password = quote_plus(Config.DB_PASSWORD)
            return f"postgresql://{Config.DB_USER}:{password}@{Config.DB_HOST}:{Config.VECTOR_PORT}/{Config.DB_NAME}"
        return f"postgresql://{Config.DB_USER}@{Config.DB_HOST}:{Config.VECTOR_PORT}/{Config.DB_NAME}"