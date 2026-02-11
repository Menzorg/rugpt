"""
RuGPT Engine Runner

Entry point for running the RuGPT engine.
"""
import uvicorn
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("rugpt")


def run():
    """Run the RuGPT engine"""
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8100"))

    logger.info(f"Starting RuGPT Engine on {host}:{port}")

    uvicorn.run(
        "src.engine.app:app",
        host=host,
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )


if __name__ == "__main__":
    run()
