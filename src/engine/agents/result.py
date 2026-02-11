"""
Agent Result

Unified result dataclass returned by all agent graph types.
"""
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class ToolCall:
    """Record of a tool invocation during agent execution"""
    tool_name: str
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ""


@dataclass
class AgentResult:
    """
    Unified result from agent execution.

    Returned by all graph types (simple, chain, multi_agent).
    """
    content: str                                        # Final text response
    model: str = ""                                     # Model used
    agent_type: str = "simple"                          # Which graph type ran
    tool_calls: List[ToolCall] = field(default_factory=list)  # Tools invoked
    tokens_used: int = 0
    finish_reason: str = "stop"                         # stop, error, timeout
    error: Optional[str] = None                         # Error message if failed
