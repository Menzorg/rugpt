"""
Token counter utility.

Loads a single tokenizers/tokenizer.json at init time and uses it for all
models.  Falls back to tiktoken cl100k_base if the file is absent.
"""
import logging
from pathlib import Path
from typing import Union

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


def count_tokens(_model: str, text: str) -> int:
    """Return the token count for *text*. *_model* is accepted but ignored."""
    global _encoder
    if _encoder is None:
        init_token_counter()

    if isinstance(_encoder, Tokenizer):
        return len(_encoder.encode(text).ids)
    # tiktoken vocabularies differ from local tokenizers — multiply to compensate
    return int(len(_encoder.encode(text)) * 1.4)
