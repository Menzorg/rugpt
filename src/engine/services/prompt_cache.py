"""
Prompt Cache

In-memory cache for role prompt files.
Reads prompts from disk on first access, caches in memory.
Supports cache clear without restart.
"""

from src.engine.unified_logger import get_logger
import os
import zoneinfo
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = get_logger("services")

_RU_WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
_RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

def _today_ru(timezone: str = "Europe/Moscow") -> str:
    now = datetime.now(tz=zoneinfo.ZoneInfo(timezone))
    return (
        f"Сегодня: {_RU_WEEKDAYS[now.weekday()]}, "
        f"{now.day} {_RU_MONTHS[now.month - 1]} {now.year}, "
        f"{now.strftime('%H:%M')} (часовой пояс: {timezone}, "
        f"ISO: {now.isoformat(timespec='minutes')})"
    )

class PromptCache:
    """
    Cache for role system prompts stored as files.

    Prompts are read from disk on first access and cached in memory.
    Cache can be cleared per-file or entirely via admin API.
    """

    def __init__(self, prompts_dir: str):
        self._cache: dict[str, str] = {}
        self._prompts_dir = prompts_dir
        self._subagents_dir = os.path.join(prompts_dir, "subagents")

    def get_prompt(self, role, org_context: str = "", is_subagent: bool = False, timezone: str = "Europe/Moscow") -> str:
        """
        Get system prompt for a role.

        Priority:
        0. prompts/subagents/<prompt_file> when is_subagent=True
        1. prompt_file (from file on disk, cached in memory)
        2. system_prompt (from DB, backward compatibility)

        If org_context is provided, it is prepended to the prompt.

        Args:
            role: Role object with prompt_file and system_prompt attributes
            org_context: Organization context to prepend to the prompt
            is_subagent: Prefer prompts/subagents/<prompt_file> before root prompts

        Returns:
            System prompt text
        """
        role_prompt = ""
        if role.prompt_file:
            prompt_file = role.prompt_file
            cache_key = f"subagents/{prompt_file}" if is_subagent else prompt_file
            path = (
                os.path.join(self._subagents_dir, prompt_file)
                if is_subagent
                else os.path.join(self._prompts_dir, prompt_file)
            )
            if cache_key not in self._cache:
                try:
                    self._cache[cache_key] = Path(path).read_text(encoding="utf-8")
                    logger.info("Loaded prompt from file: %s", cache_key)
                except FileNotFoundError:
                    if is_subagent:
                        root_path = os.path.join(self._prompts_dir, prompt_file)
                        try:
                            self._cache[prompt_file] = Path(root_path).read_text(encoding="utf-8")
                            logger.info("Loaded prompt from file: %s", prompt_file)
                        except FileNotFoundError:
                            logger.warning("Prompt file not found: %s", root_path)
                            role_prompt = role.system_prompt or ""
                    else:
                        logger.warning(f"Prompt file not found: {path}")
                        role_prompt = role.system_prompt or ""
            if not role_prompt:
                role_prompt = self._cache.get(cache_key) or self._cache.get(prompt_file, "")
        else:
            role_prompt = role.system_prompt or ""

        if "{today}" in role_prompt:
            role_prompt = role_prompt.replace("{today}", _today_ru(timezone))

        if org_context:
            return f"{org_context}\n\n---\n\n{role_prompt}"
        return role_prompt

    def clear(self, prompt_file: Optional[str] = None):
        """
        Clear cached prompts.

        Args:
            prompt_file: If provided, clear only this file's cache.
                        If None, clear entire cache.
        """
        if prompt_file:
            removed = self._cache.pop(prompt_file, None)
            if removed is not None:
                logger.info(f"Cleared prompt cache for: {prompt_file}")
            else:
                logger.info(f"Prompt not in cache: {prompt_file}")
        else:
            count = len(self._cache)
            self._cache.clear()
            logger.info(f"Cleared entire prompt cache ({count} entries)")

    @property
    def cached_count(self) -> int:
        """Number of cached prompts"""
        return len(self._cache)

    @property
    def cached_files(self) -> list[str]:
        """List of cached prompt file names"""
        return list(self._cache.keys())
