"""
Unit tests for src.engine.utils.token_counter.

The tests use the DEFAULT_MODEL from the environment (via Config) so they
exercise the same tokenizer path the rest of the engine would use at runtime.
If no local tokenizer file exists for that model the counter falls back to
tiktoken — both paths are valid and tested here.
"""
def test_count_tokens_returns_positive_int():
    """count_tokens returns a positive integer for a non-empty string."""
    from src.engine.utils.token_counter import count_tokens

    result = count_tokens("Hello, world!")
    assert isinstance(result, int)
    assert result > 0


def test_count_tokens_empty_string():
    """count_tokens returns 0 for an empty string."""
    from src.engine.utils.token_counter import count_tokens

    result = count_tokens("")
    assert result == 0


def test_count_tokens_longer_text_is_more():
    """A longer text produces a higher token count than a shorter one."""
    from src.engine.utils.token_counter import count_tokens

    short = count_tokens("Hi")
    long = count_tokens("Hi " * 100)
    assert long > short


def test_count_tokens_same_text_is_deterministic():
    """Calling count_tokens twice with the same input returns the same value."""
    from src.engine.utils.token_counter import count_tokens

    text = "Deterministic token counting test."
    assert count_tokens(text) == count_tokens(text)
