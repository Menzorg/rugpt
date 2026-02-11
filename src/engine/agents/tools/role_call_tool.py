"""
Role Call Tool

LangChain tool for calling another role from within an agent.
Enables multi-agent delegation.
"""
import logging
from langchain_core.tools import tool

logger = logging.getLogger("rugpt.agents.tools.role_call")


@tool
def role_call(role_code: str, message: str) -> str:
    """Delegate a question to another AI role.
    Args:
        role_code: Code of the role to call (e.g. "lawyer", "accountant")
        message: Message/question to send to that role
    """
    # Phase 5: will call AgentExecutor for the target role
    logger.info(f"role_call called: role_code={role_code}, message={message[:50]}...")
    return f"Delegated to {role_code}. (Cross-role calls will be active in Phase 5)"
