import inspect
import json
import logging
import os
import sys
import threading

from abc import ABC, abstractmethod
from datetime import datetime, date, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from src.engine.logging_context import get_correlation_id, get_user_id


# JSON encoder для типов из БД/моделей (Decimal/datetime/date/set).
# Fallback `str(obj)` — гарантия, что логгер не упадёт, если в **kwargs
# прилетел нестандартный объект (UUID, Enum, dataclass и т.п.).
class SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal and other non-serializable types."""
    # Конвертирует нестандартные типы; всё неизвестное → str (никогда не бросает).
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, set):
            return list(obj)
        return str(obj)  # Fallback: convert to string


# Извлекает имя caller-функции (и класса/файла) из стека вызовов.
# Применяется для авто-обогащения ERROR-логов: даёт ответ «откуда упало».
# Пропускает служебные фреймы (наш logger, asyncio, stdlib).
# Возвращает dict с ключами `handler`, `handler_class`, `handler_file`
# (любой из них может отсутствовать, если в стеке не нашёлся подходящий фрейм).
def _get_caller_handler_info(skip_frames: int = 3) -> Dict[str, Any]:
    """Извлекает имя caller-функции (и класса/файла) из стека вызовов.

    Применяется для авто-обогащения ERROR-логов: даёт ответ «откуда упало».
    Пропускает служебные фреймы (наш logger, asyncio, stdlib).

    Returns:
        dict с ключами `handler`, `handler_class`, `handler_file`
        (любой из них может отсутствовать, если в стеке не нашёлся подходящий фрейм).
    """
    result: Dict[str, Any] = {}
    skip_patterns = (
        "unified_logger",
        "logging/",
        "logging\\",
        "/lib/python",
        "\\lib\\python",
        "site-packages",
        "asyncio/",
        "asyncio\\",
    )
    try:
        stack = inspect.stack()
        for frame_info in stack[skip_frames:]:
            filename = frame_info.filename
            func_name = frame_info.function
            if any(pattern in filename for pattern in skip_patterns):
                continue
            if func_name.startswith("_"):
                continue
            result["handler"] = func_name
            if "/src/" in filename:
                result["handler_file"] = filename.split("/src/")[-1]
            elif "\\src\\" in filename:
                result["handler_file"] = filename.split("\\src\\")[-1]
            else:
                result["handler_file"] = os.path.basename(filename)
            frame_locals = frame_info.frame.f_locals
            if "self" in frame_locals:
                result["handler_class"] = frame_locals["self"].__class__.__name__
            elif "cls" in frame_locals:
                result["handler_class"] = frame_locals["cls"].__name__
            break
    except Exception:
        pass
    return result


# Formatter под jsonl-формат: одна JSON-запись на строку.
# В каждую запись подмешивает correlation_id и user_id из ContextVar (если выставлены)
# и `metadata` из record.extra (туда `_log` кладёт **kwargs вызывающего).
class StructuredFormatter(logging.Formatter):
    # Имя компонента запоминается и попадает в поле `component` каждой записи.
    def __init__(self, component: str):
        super().__init__()
        self.component = component

    # Собирает dict из record + ContextVar + metadata и сериализует в одну JSON-строку.
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "component": self.component,
            "message": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid and cid != "-":
            log_data["correlation_id"] = cid
        uid = get_user_id()
        if uid:
            log_data["user_id"] = uid
        metadata = getattr(record, "metadata", None)
        if metadata:
            log_data["metadata"] = metadata
        return json.dumps(log_data, ensure_ascii=False, cls=SafeJSONEncoder)


# Базовый per-component logger: stdout + jsonl FileHandler, ротация по дням,
# авто-обогащение ERROR+ caller-инфой. Подкласс задаёт только путь к директории
# (`_get_log_directory`) и шаблон имени (`build_logger_name`) — общая логика здесь.
class BaseUnifiedLogger(ABC):
    # Создаёт per-component logger: stdout + jsonl `<dir>/<component>.jsonl`.
    # - `handlers.clear()` — защита от дубля: повторный `getLogger` с тем же именем
    #   возвращает тот же объект, и без очистки на нём накапливались бы FileHandler'ы
    #   (запись задваивалась/затраивалась).
    # - `propagate=False` — иначе запись уходит и в root logger, что даёт второй вывод
    #   через basicConfig.
    # - `current_date` запоминается для последующего сравнения в `_check_and_rotate_handlers`.
    def __init__(self, component: str):
        self.component = component
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        self.lock = threading.Lock()
        logger_name = self.build_logger_name(component)
        self.logger = logging.getLogger(logger_name)
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(StructuredFormatter(self.component))
        self.logger.addHandler(ch)

        log_dir = self._get_log_directory()
        log_file = log_dir / f"{self.component}.jsonl"
        fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        fh.setFormatter(StructuredFormatter(self.component))
        self.logger.addHandler(fh)

    # Внутренняя точка входа всех уровней: ротация → авто caller-инфа на ERROR+ →
    # запись с **kwargs в JSON-поле `metadata`.
    # Авто-обогащение на ERROR+ нужно, чтобы при упавшем запросе сразу было видно
    # «откуда упало», не ныряя в traceback. Если caller сам передал `handler=...` — не трогаем.
    def _log(self, level: int, message: str, **kwargs) -> None:
        self._check_and_rotate_handlers()
        # На ERROR+ автоматически обогащаем kwargs информацией о caller'е,
        # если её ещё не передали явно — для быстрой ориентации в логах.
        if level >= logging.ERROR and "handler" not in kwargs:
            kwargs.update(_get_caller_handler_info())
        extra = {"metadata": kwargs} if kwargs else None
        self.logger.log(level, message, extra=extra)

    # DEBUG; **kwargs → JSON-поле `metadata`.
    def debug(self, message: str, **kwargs) -> None:
        self._log(logging.DEBUG, message, **kwargs)

    # INFO; **kwargs → JSON-поле `metadata`.
    def info(self, message: str, **kwargs) -> None:
        self._log(logging.INFO, message, **kwargs)

    # WARNING; **kwargs → JSON-поле `metadata`.
    def warning(self, message: str, **kwargs) -> None:
        self._log(logging.WARNING, message, **kwargs)

    # ERROR; **kwargs → JSON-поле `metadata`, плюс автоматически добавляется
    # caller-инфа (см. `_log`).
    def error(self, message: str, **kwargs) -> None:
        self._log(logging.ERROR, message, **kwargs)

    # На смене даты закрывает старый FileHandler и открывает новый
    # в `<new_date>/<component>.jsonl`.
    # Double-checked locking:
    # - первая проверка без lock'а — быстрый путь (99.9% вызовов: дата та же,
    #   моментально выходим);
    # - повторная проверка уже под lock'ом — чтобы из двух потоков, прорвавшихся
    #   одновременно через первую проверку на границе суток, ротация выполнилась
    #   ровно один раз и на logger'е не оказалось двух FileHandler'ов, пишущих
    #   в один файл.
    def _check_and_rotate_handlers(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.current_date == today:
            return
        with self.lock:
            if today == self.current_date:
                return
            self.current_date = today
            for h in list(self.logger.handlers):
                if isinstance(h, logging.FileHandler):
                    try:
                        h.close()
                    except Exception:
                        pass
                    self.logger.removeHandler(h)
            log_dir = self._get_log_directory()
            log_file = log_dir / f"{self.component}.jsonl"
            fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            fh.setFormatter(StructuredFormatter(self.component))
            self.logger.addHandler(fh)

    # Путь к папке с сегодняшними логами (подкласс создаёт её при необходимости).
    @abstractmethod
    def _get_log_directory(self) -> Path:
        """Return the path to today's log folder."""

    # Полное имя логгера для `logging.getLogger` (например, `rugpt.<component>`).
    @abstractmethod
    def build_logger_name(self, component: str) -> str:
        """Construct the full logger name for getLogger."""
