"""
Ollama LLM Provider

Provider for local Ollama models (compatible with vLLM).
"""
import logging
import httpx
from typing import List, Optional

from .base import BaseLLMProvider, LLMMessage, LLMResponse
from ...config import Config

logger = logging.getLogger("rugpt.llm.ollama")


class OllamaProvider(BaseLLMProvider):
    """
    Ollama LLM provider for local models.

    Supports:
    - Ollama API (http://localhost:11434)
    - vLLM with OpenAI-compatible API
    - Any OpenAI-compatible local server
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: float = 300.0  # 5 minutes for CPU
    ):
        """
        Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL
            default_model: Default model to use
            timeout: Request timeout in seconds
        """
        self.base_url = base_url or Config.LLM_BASE_URL
        self.default_model = default_model or Config.DEFAULT_MODEL
        self.timeout = timeout
        # Configure explicit timeout for CPU inference
        timeout_config = httpx.Timeout(timeout, connect=30.0)
        self.client = httpx.AsyncClient(timeout=timeout_config)
        logger.info(f"OllamaProvider initialized: base_url={self.base_url}, model={self.default_model}, timeout={timeout}s")

    async def generate(
        self,
        messages: List[LLMMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048
    ) -> LLMResponse:
        """Generate response using Ollama API"""
        model_name = model or self.default_model
        logger.info(f"Generating with model={model_name}, messages={len(messages)}, max_tokens={max_tokens}")

        # Format messages for Ollama
        formatted_messages = [
            {"role": msg.role, "content": msg.content}
            for msg in messages
        ]

        try:
            # Try chat endpoint first (Ollama native)
            logger.info(f"Sending request to {self.base_url}/api/chat...")
            response = await self.client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model_name,
                    "messages": formatted_messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens
                    }
                }
            )

            if response.status_code == 200:
                data = response.json()
                return LLMResponse(
                    content=data.get("message", {}).get("content", ""),
                    model=model_name,
                    tokens_used=data.get("eval_count", 0),
                    finish_reason="stop"
                )

            # Fallback to OpenAI-compatible endpoint
            response = await self.client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": model_name,
                    "messages": formatted_messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )

            if response.status_code == 200:
                data = response.json()
                choice = data.get("choices", [{}])[0]
                return LLMResponse(
                    content=choice.get("message", {}).get("content", ""),
                    model=model_name,
                    tokens_used=data.get("usage", {}).get("total_tokens", 0),
                    finish_reason=choice.get("finish_reason", "stop")
                )

            logger.error(f"LLM request failed: {response.status_code} - {response.text}")
            return LLMResponse(
                content="[Error: LLM request failed]",
                model=model_name,
                finish_reason="error"
            )

        except httpx.TimeoutException:
            logger.error(f"LLM request timed out after {self.timeout}s")
            return LLMResponse(
                content="[Error: Request timed out]",
                model=model_name,
                finish_reason="timeout"
            )
        except Exception as e:
            logger.error(f"LLM request error: {e}")
            return LLMResponse(
                content=f"[Error: {str(e)}]",
                model=model_name,
                finish_reason="error"
            )

    async def health_check(self) -> bool:
        """Check if Ollama is available"""
        try:
            response = await self.client.get(f"{self.base_url}/api/tags")
            return response.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> List[str]:
        """List available models"""
        try:
            response = await self.client.get(f"{self.base_url}/api/tags")
            if response.status_code == 200:
                data = response.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
        return []

    async def close(self):
        """Close HTTP client"""
        await self.client.aclose()
