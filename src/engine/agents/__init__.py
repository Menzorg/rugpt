"""
RuGPT Agent System

Agent execution layer: routes requests through simple/chain/multi_agent graphs.
"""
from .executor import AgentExecutor
from .result import AgentResult

__all__ = ['AgentExecutor', 'AgentResult']
