You are compacting an AI agent's conversation history to free context space.
Read the messages below and produce a dense summary the agent can use to continue working without losing critical state.

Extract and preserve:
1. **User intent** — what the user is trying to accomplish (one sentence, precise).
2. **Facts and constraints** — every concrete fact, number, date, name, rule, or constraint stated by the user or discovered by the agent.
3. **Document IDs** — every file_id (UUID) found or referenced in tool results. List each on its own line as `doc_id: <uuid> (<filename if known>)`.
4. **Requested output format** — any formatting instructions (language, structure, length, style) the user specified.
5. **Work done** — what the agent already completed or confirmed so the agent does not repeat it.
6. **Open items** — what still needs to be done to fulfil the user's request.
7. **Most relevant chunks** - really relevant results of `rag_search`.

Rules:
- Write in the same language as the user's messages.
- Be maximally concise — no filler, no hedging.
- Do not omit any document IDs or facts; losing them would force redundant tool calls.

Messages to summarize:
{messages}
