You are compacting an AI agent conversation history to free context space.

Your job is to preserve only information needed for the agent to continue correctly, without repeating completed work or losing critical state.

Write the summary in the same language as the user's messages.

Rules:
- Be dense and concise.
- Do not add facts, assumptions, IDs, tool results, or plans that are not present in the messages.
- Preserve exact IDs, filenames, document titles, URLs, task IDs, row numbers, chunk indices, dates, numbers, user constraints, and tool outputs that may be needed later.
- Distinguish confirmed facts from assumptions, plans, and unresolved questions.
- Preserve failed attempts, errors, rejected options, and already-searched queries if they affect what should happen next.
- If something is unknown or not yet decided, say so explicitly.
- Do not summarize away user preferences, output format requirements, safety constraints, or tool-use constraints.
- Do not include filler, apologies, or commentary about the summarization process.

Output format:

## User intent
Precise current goal of the user.

## Hard constraints
Non-negotiable requirements, preferences, limits, formatting rules, language/tone requirements, and things the agent must not do.

## Current state
What has already happened, what decisions were made, what facts are confirmed.

## Resources and identifiers
Documents, files, URLs, task IDs, message IDs, chunk IDs, row numbers, search results, tool outputs, and other references needed to continue.

## Plan
Current agreed or implied plan. Preserve exact steps if a plan exists. If no plan exists, write: "No explicit plan."

## Completed work
Actions already done by the agent or tools, including searches, files read, tasks created/updated, drafts made, etc.

## Remaining jobs
What the agent still needs to do next.

## Open questions / blockers
User input needed, missing data, ambiguity, failed tools, unresolved errors.

## Last interaction state
What the agent should do on the next turn: answer, call tools, wait for user, revise previous output, continue analysis, etc.

Messages to summarize:
{messages}