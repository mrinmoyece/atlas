"""Agent prompts and output parsing.

There is no `Agent` class here on purpose: an agent in Atlas *is* a
(prompt, pattern, toolkit, category) tuple executed by a graph node. Adding
a class wrapper around that would be ceremony without behaviour.
"""

from atlas.agents.parsing import ParseOutcome, parse_specialist_output
from atlas.agents.prompts import SUPERVISOR_PROMPT, specialist_prompt

__all__ = [
    "ParseOutcome",
    "parse_specialist_output",
    "SUPERVISOR_PROMPT",
    "specialist_prompt",
]
