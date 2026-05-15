"""
Prompt Cache

In-memory cache for role prompt files.
Reads prompts from disk on first access, caches in memory.
Supports cache clear without restart.
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rugpt.services.prompt_cache")

_RU_WEEKDAYS = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]
_RU_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _today_ru() -> str:
    now = datetime.now()
    return (
        f"Сегодня: {_RU_WEEKDAYS[now.weekday()]}, "
        f"{now.day} {_RU_MONTHS[now.month - 1]} {now.year} "
        f"(ISO: {now.date().isoformat()})"
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
        self._helpers_dir = os.path.join(prompts_dir, "helpers")

    def get_prompt(self, role, org_context: str = "") -> str:
        """
        Get system prompt for a role.

        Priority:
        1. prompt_file (from file on disk, cached in memory)
        2. system_prompt (from DB, backward compatibility)

        If org_context is provided, it is prepended to the prompt.

        Args:
            role: Role object with prompt_file and system_prompt attributes
            org_context: Organization context to prepend to the prompt

        Returns:
            System prompt text
        """
        role_prompt = ""
        if role.prompt_file:
            if role.prompt_file not in self._cache:
                path = os.path.join(self._prompts_dir, role.prompt_file)
                try:
                    self._cache[role.prompt_file] = Path(path).read_text(encoding="utf-8")
                    logger.info(f"Loaded prompt from file: {role.prompt_file}")
                except FileNotFoundError:
                    logger.warning(f"Prompt file not found: {path}")
                    role_prompt = role.system_prompt or ""
            if not role_prompt:
                role_prompt = self._cache.get(role.prompt_file, "")
        else:
            role_prompt = role.system_prompt or ""

        if "{today}" in role_prompt:
            role_prompt = role_prompt.replace("{today}", _today_ru())

        if org_context:
            return f"{org_context}\n\n---\n\n{role_prompt}"
        return role_prompt

    def get_helper_prompt(self, helper_name: str) -> str:
        """
        Get system prompt for a helper by name.

        Reads from prompts/helpers/<helper_name>.md, cached in memory.
        Returns empty string if the file is not found.
        """
        cache_key = f"helpers/{helper_name}.md"
        if cache_key not in self._cache:
            path = os.path.join(self._helpers_dir, f"{helper_name}.md")
            try:
                self._cache[cache_key] = Path(path).read_text(encoding="utf-8")
                logger.info("Loaded helper prompt: %s", cache_key)
            except FileNotFoundError:
                logger.warning("Helper prompt file not found: %s", path)
                self._cache[cache_key] = ""
        prompt = self._cache.get(cache_key, "")
        if "{today}" in prompt:
            prompt = prompt.replace("{today}", _today_ru())
        return prompt

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
