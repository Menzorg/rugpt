"""
Rule Generator Graph

LangGraph graph that generates a concise rule_text from:
- user_question: what was asked
- ai_answer: what the AI said (wrong)
- correction_text: what the user said was wrong

Output: a clear, concise instruction for the AI role to follow in the future.
"""
import logging

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_ollama import ChatOllama

logger = logging.getLogger("rugpt.agents.graphs.rule_generator")

RULE_GENERATOR_PROMPT = """You are a rule formulator for an AI assistant system.

Your task: given a failed AI response and user's correction, formulate a clear, concise RULE
that the AI assistant should follow in the future to avoid this mistake.

Requirements for the rule:
- Write in the same language as the correction (usually Russian)
- Be specific and actionable
- Focus on WHAT to do correctly, not what was wrong
- Keep it to 1-3 sentences
- Format: "When [situation], [correct action/answer]."

Example:
Question: "What is the statute of limitations for labor disputes?"
AI answer: "The statute of limitations for labor disputes is 3 years."
Correction: "Wrong! For labor disputes it's 3 months under Article 392 of the Labor Code."
Rule: "When asked about the statute of limitations for labor disputes: the deadline is 3 months (Article 392 of the Labor Code of the Russian Federation), not 3 years as in general civil cases."
"""


async def generate_rule_text(
    base_url: str,
    model: str,
    user_question: str,
    ai_answer: str,
    correction_text: str,
    temperature: float = 0.3,
) -> str:
    """
    Generate a concise rule from a correction.

    Args:
        base_url: LLM API base URL
        model: Model name
        user_question: Original user question
        ai_answer: AI's incorrect response
        correction_text: User's correction/feedback

    Returns:
        Generated rule text string
    """
    llm = ChatOllama(
        base_url=base_url,
        model=model,
        temperature=temperature,
    )

    user_content = (
        f"Question: {user_question}\n\n"
        f"AI answer: {ai_answer}\n\n"
        f"User correction: {correction_text}\n\n"
        f"Formulate a rule:"
    )

    messages = [
        SystemMessage(content=RULE_GENERATOR_PROMPT),
        HumanMessage(content=user_content),
    ]

    try:
        response = await llm.ainvoke(messages)
        rule_text = response.content if hasattr(response, 'content') else str(response)
        logger.info(f"Generated rule_text: {rule_text[:100]}...")
        return rule_text.strip()

    except Exception as e:
        logger.error(f"Rule text generation failed: {e}")
        return ""
