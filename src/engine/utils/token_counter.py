"""
Token counter utility.

Loads a single tokenizers/tokenizer.json at init time and uses it for all
models.  Falls back to tiktoken cl100k_base if the file is absent.
"""
import logging
from pathlib import Path
from typing import Iterable, Union

from langchain_core.messages import BaseMessage

import tiktoken
from tokenizers import Tokenizer

logger = logging.getLogger("rugpt.utils.token_counter")

_TOKENIZER_PATH = Path(__file__).parent.parent.parent.parent / "tokenizers" / "tokenizer.json"
_TIKTOKEN_FALLBACK = "cl100k_base"

_encoder: Union[Tokenizer, tiktoken.Encoding, None] = None


def init_token_counter() -> None:
    """Load tokenizers/tokenizer.json; fall back to tiktoken if absent."""
    global _encoder
    if _TOKENIZER_PATH.exists():
        try:
            _encoder = Tokenizer.from_file(str(_TOKENIZER_PATH))
            logger.info("Loaded tokenizer from %s", _TOKENIZER_PATH)
            return
        except Exception:
            logger.exception("Failed to load tokenizer from %s", _TOKENIZER_PATH)

    logger.warning("tokenizer.json not found at %s, falling back to tiktoken %s", _TOKENIZER_PATH, _TIKTOKEN_FALLBACK)
    _encoder = tiktoken.get_encoding(_TIKTOKEN_FALLBACK)


def count_tokens(text: str, tool_count: int = 0) -> int:
    """Return the token count for *text* plus an estimate for tool schemas.

    Each tool schema adds ~150 tokens of overhead to the context window.
    Pass tool_count to include that overhead in the estimate.
    """
    global _encoder
    if _encoder is None:
        init_token_counter()

    if isinstance(_encoder, Tokenizer):
        text_tokens = len(_encoder.encode(text).ids)
    else:
        # tiktoken vocabularies differ from local tokenizers — multiply to compensate
        text_tokens = int(len(_encoder.encode(text)) * 0.8)

    return text_tokens + tool_count * 150


def cut_text_by_token_count(text: str, limit: int) -> str:
    """Return *text* truncated to at most *limit* tokens.

    Strategy:
    1. Pre-slice to limit * 10 characters to avoid encoding a huge string.
    2. Encode the pre-sliced text with the loaded tokenizer.
    3. Decode only the first *limit* token IDs back to a string.
    """
    global _encoder
    if _encoder is None:
        init_token_counter()

    # Pre-slice: one token is rarely longer than 10 chars, so this is a safe upper bound.
    candidate = text[: limit * 10]

    if isinstance(_encoder, Tokenizer):
        ids = _encoder.encode(candidate).ids[:limit]
        return _encoder.decode(ids)
    else:
        # tiktoken: encode returns a list of ints; decode accepts the same.
        ids = _encoder.encode(candidate)[:limit]
        return _encoder.decode(ids)


def count_tokens_messages(messages: Iterable[BaseMessage]) -> int:
    """Return the total token count for an iterable of BaseMessage objects."""
    parts = []
    for msg in messages:
        content = msg.content
        if isinstance(content, list):
            parts.extend(
                item["text"] if isinstance(item, dict) and "text" in item else str(item)
                for item in content
            )
        else:
            parts.append(str(content))
    return count_tokens("\n".join(parts))
