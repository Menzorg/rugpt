from datetime import datetime
from pathlib import Path

from .base import BaseUnifiedLogger


# Реализация `BaseUnifiedLogger` для движка rugpt.
# Логи лежат в `<repo>/logs/engine/<today>/<component>.jsonl`,
# имя логгера — `rugpt.<component>`.
class EngUnifiedLogger(BaseUnifiedLogger):
    # Возвращает (и при необходимости создаёт) папку под сегодняшние логи.
    # 4 parent'а — это путь от `.../src/engine/unified_logger/eng.py` до корня репо.
    # `mkdir(parents=True, exist_ok=True)` — папка дня создаётся при первом вызове;
    # при ротации на следующих сутках та же логика создаст новую папку.
    def _get_log_directory(self) -> Path:
        project_root = Path(__file__).parent.parent.parent.parent
        today = datetime.now().strftime("%Y-%m-%d")
        log_dir = project_root / "logs" / "engine" / today
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    # `rugpt.<component>` — общий префикс для всех наших логгеров.
    # Даёт две вещи:
    # 1) одной строкой `logging.getLogger('rugpt')` можно отфильтровать всё своё;
    # 2) stdlib-логгеры (uvicorn/asyncpg/aiokafka) остаются под другими префиксами
    #    и не смешиваются с нашими.
    def build_logger_name(self, component: str) -> str:
        return f"rugpt.{component}"
