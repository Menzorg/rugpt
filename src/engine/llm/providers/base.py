"""
Base LLM Provider

Abstract base class for LLM providers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LLMMessage:
    """Message for LLM conversation"""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class LLMResponse:
    """Response from LLM"""
    content: str
    model: str
    tokens_used: int = 0
    finish_reason: str = "stop"


class BaseLLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations:
    - OllamaProvider: Local Ollama models (vLLM compatible)
    - OpenAIProvider: OpenAI API fallback
    """

    @abstractmethod
    async def generate(
        self,
        messages: List[LLMMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048
    ) -> LLMResponse:
        """
        Generate a response from the LLM.

        Args:
            messages: Conversation history
            model: Model name (uses default if not specified)
            temperature: Sampling temperature
            max_tokens: Maximum tokens in response

        Returns:
            LLMResponse with generated text
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the LLM provider is available"""
        pass

    @abstractmethod
    async def list_models(self) -> List[str]:
        """List available models"""
        pass
