"""
RuGPT Engine Runner

Entry point for running the RuGPT engine.
"""
import json
import uvicorn
import logging
import os
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Load .env file
load_dotenv(Path(__file__).parent.parent.parent / ".env")


class DailyDirJsonlHandler(logging.Handler):
    """Logs go to <base>/YYYY-MM-DD/engine.jsonl, одна запись = одна JSON-строка.

    Папка создаётся при первой записи дня. День определяется по текущей системной
    дате (UTC) на момент emit; на смене дня старый поток закрывается, новый открывается.
    Без size-rotation, без retention — файлы растут до ручной архивации.
    """
    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = base_dir
        self._current_date: str | None = None
        self._stream = None

    def _ensure_stream(self):
        today = _date.today().isoformat()
        if today == self._current_date and self._stream and not self._stream.closed:
            return
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
        day_dir = self.base_dir / today
        day_dir.mkdir(parents=True, exist_ok=True)
        # buffering=1 = line-buffered → каждая запись сразу видна tail -f
        self._stream = open(day_dir / "engine.jsonl", "a", buffering=1, encoding="utf-8")
        self._current_date = today

    def emit(self, record: logging.LogRecord):
        try:
            self._ensure_stream()
            payload = {
                "ts": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = logging.Formatter().formatException(record.exc_info)
            self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
        super().close()


# Configure logging — StreamHandler (для screen) + DailyDirJsonlHandler (логи по дням)
LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), DailyDirJsonlHandler(LOG_DIR)],
)
logger = logging.getLogger("rugpt")


def run(): #start
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
