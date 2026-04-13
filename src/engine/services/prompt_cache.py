"""
Prompt Cache

In-memory cache for role prompt files.
Reads prompts from disk on first access, caches in memory.
Supports cache clear without restart.
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("rugpt.services.prompt_cache")


class PromptCache:
    """
    Cache for role system prompts stored as files.

    Prompts are read from disk on first access and cached in memory.
    Cache can be cleared per-file or entirely via admin API.
    """

    def __init__(self, prompts_dir: str):
        self._cache: dict[str, str] = {}
        self._prompts_dir = prompts_dir

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
