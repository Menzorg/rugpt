You are compacting an AI agent's conversation history to free context space.
Read the messages below and produce a dense summary the agent can use to continue working without losing critical state.

## Instructions:
1. Extract user intent — what the user is trying to accomplish (precise).
2. Extract Current plan state - if user and agent has a plan - copy it precisely as instructions.
3. Extract Resources - what was done already, which documents found to work with.
4. Extract Jobs - what is left to be done by agent.
5. Form a final sumamry as output.

## Format:
- Write in the same language as the user's messages.
- Be maximally concise — no filler, no hedging.
- Do not omit any document IDs or facts; losing them would force redundant tool calls.

Messages to summarize:
{messages}
