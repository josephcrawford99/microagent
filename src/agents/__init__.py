"""Harnesses are the loop that owns prompts, parsing, and routing.
A harness watches every service's `out` handle, wakes a Provider with a
batch of messages, and routes the structured reply back into `in` handles.
Can add different ones for different workflows

`base_agent.py` has the plumbing every harness shares (the harness's own
`run/agent/` dir, buffering, the coalesce-and-wake loop, channel lookup,
unaddressed-text and error notices); a harness subclasses `BaseAgent` and
implements `wake`.
"""
