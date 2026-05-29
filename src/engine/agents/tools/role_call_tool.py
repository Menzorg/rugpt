"""
Role Call Tool

LangChain tool for calling another role from within an agent.
Enables multi-agent delegation.
"""

from src.engine.unified_logger import get_logger
from langchain_core.tools import tool

logger = get_logger("agents")
_TOOL_ERROR_RESULT = "Tool execution caused errors. No result"

@tool
def role_call(role_code: str, message: str) -> str:
    """Delegate a question to another AI role.
    Args:
        role_code: Code of the role to call (e.g. "lawyer", "accountant")
        message: Message/question to send to that role
    """
    try:
        # Phase 5: will call AgentExecutor for the target role
        logger.info(f"role_call called: role_code={role_code}, message={message[:50]}...")
        return f"Delegated to {role_code}. (Cross-role calls will be active in Phase 5)"
    except Exception as e:
        logger.error(f"role_call failed: {e}", exc_info=True)
        return _TOOL_ERROR_RESULT
