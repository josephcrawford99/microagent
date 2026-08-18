"""LLM providers — one file per vendor, text in -> text out, nothing else.

A vendor file implements one method: `generate(prompt) -> str`. One string in,
one string out — the harness builds the whole prompt. What it does
internally is its own business (the claude CLI runs its own tools in space/ —
a human as an API); to the harness it's always input -> response.

Parsing the reply into the shared envelope is the harness's job
(`lib/message.parse_reply`), not the provider's.
"""
