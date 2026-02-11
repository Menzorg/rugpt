"""
Agent Executor

Main router: dispatches execution to the right graph based on role.agent_type.
"""
import logging
from typing import List, Optional

from langchain_ollama import ChatOllama

from ..models.role import Role
from ..services.prompt_cache import PromptCache
from .result import AgentResult
from .tools.registry import ToolRegistry
from .graphs.simple import run_simple_agent
from .graphs.chain import run_chain_agent
from .graphs.multi_agent import run_multi_agent

logger = logging.getLogger("rugpt.agents.executor")


class AgentExecutor:
    """
    Central agent executor.

    Routes requests to the appropriate graph based on role.agent_type:
    - "simple": direct LLM or ReAct agent (if tools present)
    - "chain": sequential steps from agent_config["steps"]
    - "multi_agent": LangGraph StateGraph from agent_config["graph"]
    """

    def __init__(
        self,
        base_url: str,
        default_model: str,
        prompt_cache: PromptCache,
        tool_registry: Optional[ToolRegistry] = None,
        timeout: float = 300.0,
    ):
        self.base_url = base_url
        self.default_model = default_model
        self.prompt_cache = prompt_cache
        self.tool_registry = tool_registry or ToolRegistry()
        self.timeout = timeout

    def _create_llm(self, model: str, temperature: float = 0.7) -> ChatOllama:
        """Create a ChatOllama instance for the given model"""
        return ChatOllama(
            base_url=self.base_url,
            model=model,
            temperature=temperature,
            # Ollama-specific timeout handled via request_timeout
        )

    async def execute(
        self,
        role: Role,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AgentResult:
        """
        Execute agent for a role.

        Args:
            role: Role with agent_type, agent_config, tools, prompt_file
            messages: Conversation as [{"role": "user"/"assistant", "content": "..."}]
            temperature: Sampling temperature
            max_tokens: Max tokens in response

        Returns:
            AgentResult with response
        """
        model = role.model_name or self.default_model
        system_prompt = self.prompt_cache.get_prompt(role)
        tools = self.tool_registry.resolve(role.tools) if role.tools else []
        llm = self._create_llm(model, temperature)

        logger.info(
            f"Executing agent: role={role.code}, type={role.agent_type}, "
            f"model={model}, tools={len(tools)}"
        )

        try:
            if role.agent_type == "simple":
                return await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools if tools else None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

            elif role.agent_type == "chain":
                return await run_chain_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    agent_config=role.agent_config,
                    tools=tools if tools else None,
                )

            elif role.agent_type == "multi_agent":
                return await run_multi_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    agent_config=role.agent_config,
                    tools=tools if tools else None,
                )

            else:
                logger.warning(f"Unknown agent_type '{role.agent_type}', falling back to simple")
                return await run_simple_agent(
                    llm=llm,
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )

        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            return AgentResult(
                content=f"[Error: {e}]",
                model=model,
                agent_type=role.agent_type,
                finish_reason="error",
                error=str(e),
            )
