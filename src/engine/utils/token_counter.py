"""
Token counter utility.

Primary path: per-model tokenizer downloaded from HuggingFace Hub into
  tokenizers/<model_name>/  where model_name is the part after '/' in the
  repo id (e.g. "Qwen2.5-7B-Instruct" from "Qwen/Qwen2.5-7B-Instruct").
  Only tokenizer files are downloaded — no weights.
  The directory is reused on subsequent runs without hitting the network.

Fallback: tiktoken cl100k_base (used when no model-specific tokenizer is
  available or when called without a model ID).
"""
import logging
import re
from pathlib import Path
from typing import Iterable, Union

from langchain_core.messages import BaseMessage

import tiktoken
from tokenizers import Tokenizer

logger = logging.getLogger("rugpt.utils.token_counter")

_TOKENIZERS_DIR = Path(__file__).parent.parent.parent.parent / "tokenizers"
_TIKTOKEN_FALLBACK = "cl100k_base"

# Maps model_id -> loaded Tokenizer (or tiktoken Encoding).
_model_cache: dict[str, Union[Tokenizer, tiktoken.Encoding]] = {}

_DOWNLOAD_PATTERNS = [
    "*.json",
    "*.jinja",
    "*.txt",
    "*.model",
    "tokenizer*",
    "vocab.*",
    "merges.txt",
    "added_tokens.json",
    "special_tokens_map.json",
]
_DOWNLOAD_IGNORE = [
    "*.safetensors", "*.bin", "*.pt", "*.pth", "*.ckpt",
    "*.gguf", "*.onnx", "*.h5", "*.msgpack",
]

_tiktoken_enc: tiktoken.Encoding | None = None


def _get_tiktoken() -> tiktoken.Encoding:
    global _tiktoken_enc
    if _tiktoken_enc is None:
        _tiktoken_enc = tiktoken.get_encoding(_TIKTOKEN_FALLBACK)
    return _tiktoken_enc


def _get_default_tokenizer() -> Union[Tokenizer, tiktoken.Encoding]:
    """Return the tokenizer for DEFAULT_MODEL (cached), tiktoken on failure."""
    try:
        from ..config import Config
        return get_tokenizer_for_model(Config.DEFAULT_MODEL)
    except Exception:
        logger.warning("token_counter: failed to load default model tokenizer, using tiktoken")
        return _get_tiktoken()


def _model_dir_name(model_id: str) -> str:
    """'Qwen/Qwen2.5-7B-Instruct' -> 'Qwen2.5-7B-Instruct'"""
    name = model_id.split("/")[-1]
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)


def _load_tokenizer_for_model(model_id: str) -> Union[Tokenizer, tiktoken.Encoding]:
    """
    Return a tokenizer for *model_id*, downloading from HuggingFace Hub if
    not already cached locally. Falls back to tiktoken on any error.
    """
    cache_dir = _TOKENIZERS_DIR / _model_dir_name(model_id)
    tokenizer_file = cache_dir / "tokenizer.json"

    if tokenizer_file.exists():
        try:
            enc = Tokenizer.from_file(str(tokenizer_file))
            logger.info("token_counter: loaded cached tokenizer for %s from %s", model_id, cache_dir)
            return enc
        except Exception:
            logger.exception("token_counter: failed to load cached tokenizer for %s", model_id)

    logger.info("token_counter: downloading tokenizer for %s into %s", model_id, cache_dir)
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(
            repo_id=model_id,
            local_dir=str(cache_dir),
            allow_patterns=_DOWNLOAD_PATTERNS,
            ignore_patterns=_DOWNLOAD_IGNORE,
        )
    except Exception:
        logger.exception("token_counter: snapshot_download failed for %s, falling back to tiktoken", model_id)
        return _get_tiktoken()

    if tokenizer_file.exists():
        try:
            enc = Tokenizer.from_file(str(tokenizer_file))
            logger.info("token_counter: tokenizer for %s downloaded and loaded", model_id)
            return enc
        except Exception:
            logger.exception("token_counter: failed to load downloaded tokenizer for %s", model_id)

    logger.warning(
        "token_counter: no tokenizer.json for %s after download, falling back to tiktoken", model_id,
    )
    return _get_tiktoken()


def get_tokenizer_for_model(model_id: str) -> Union[Tokenizer, tiktoken.Encoding]:
    """Return (and cache in-process) a tokenizer for *model_id*."""
    if model_id not in _model_cache:
        _model_cache[model_id] = _load_tokenizer_for_model(model_id)
    return _model_cache[model_id]


def init_token_counter() -> None:
    """Pre-warm the tokenizer for Config.DEFAULT_MODEL at startup.

    Downloads and caches the tokenizer if not already on disk so that the
    first real token count does not block a request.
    """
    from ..config import Config
    model_id = Config.DEFAULT_MODEL
    logger.info("token_counter: pre-warming tokenizer for default model %s", model_id)
    get_tokenizer_for_model(model_id)


_LATIN_RE = re.compile(r'[a-zA-Z]')


def _encode_with(enc: Union[Tokenizer, tiktoken.Encoding], text: str) -> int:
    """Count tokens using whichever encoder is provided.

    For tiktoken, applies a Cyrillic/Latin ratio heuristic to compensate for
    tiktoken's byte-level inefficiency on non-Latin scripts.
    """
    if not text:
        return 0
    if isinstance(enc, Tokenizer):
        return len(enc.encode(text).ids)
    # tiktoken fallback: heuristic correction for Cyrillic/Latin ratio.
    # These heuristics are rough — they are better than nothing in emergencies
    # (primary tokenizer failed; count_tokens logs a warning when this path fires),
    # but they are not accurate. Currently tuned for Cyrillic + Latin only.
    # TODO: when we start working with languages beyond Cyrillic/Latin (CJK, Arabic,
    #       Hebrew, etc.) these heuristics should be extended or replaced with
    #       per-script detection so non-Latin scripts don't silently get the
    #       Cyrillic 0.4 compression factor applied to them.
    if not _LATIN_RE.search(text):
        return int(len(enc.encode(text)) * 0.4)
    cyrillic = sum(1 for c in text if 'Ѐ' <= c <= 'ӏ') * 2
    latin = sum(1 for c in text if 'a' <= c <= 'z' or 'A' <= c <= 'Z')
    total_script = cyrillic + latin
    ratio = cyrillic / total_script if total_script else 1.0
    coef = ratio * 0.4 + (1 - ratio) * 1.0
    return int(len(enc.encode(text)) * coef)


def count_tokens(text: str, tool_count: int = 0) -> int:
    """Return the token count for *text* plus an estimate for tool schemas.

    Primary: DEFAULT_MODEL tokenizer (tokenizers lib).
    Fallback: tiktoken cl100k_base on any error.
    Each tool schema adds 200 tokens of overhead to the context window.
    Pass tool_count to include that overhead in the estimate.
    """
    if not text:
        return tool_count * 200
    try:
        enc = _get_default_tokenizer()
        return _encode_with(enc, text) + tool_count * 200
    except Exception:
        logger.warning("token_counter: count_tokens fell back to tiktoken")
        return _encode_with(_get_tiktoken(), text) + tool_count * 200


def cut_text_by_token_count(text: str, limit: int) -> str:
    """Return *text* truncated to at most *limit* tokens."""
    try:
        enc = _get_default_tokenizer()
        if isinstance(enc, Tokenizer):
            ids = enc.encode(text).ids[:limit]
            return enc.decode(ids)
    except Exception:
        logger.warning("token_counter: cut_text_by_token_count fell back to tiktoken")
    enc_tk = _get_tiktoken()
    candidate = text[: limit * 10]
    ids = enc_tk.encode(candidate)[:limit]
    return enc_tk.decode(ids)


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
