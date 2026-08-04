"""Graph state and reducers.

LangGraph merges state updates from parallel branches using *reducers*. Get
these wrong and concurrent specialists silently clobber each other - the
single most common multi-agent bug, and one that only shows up under
parallel load.

The rules encoded here:

  * `findings` and `results` **accumulate** (list concat). Four specialists
    finishing at once must all contribute.
  * `tokens_used` / `cost_usd` **sum**. Cost is the one number a business
    always asks about; losing an update means under-reporting spend.
  * `errors` **merge by key**. Specialists are isolated: one failing must
    not fail the run, it must be recorded and reported.
  * Scalars the supervisor owns (`repo`, `plan`, `verdict`) use
    last-write-wins, because only one node writes them.

Note what is NOT in shared state: specialist transcripts. Each specialist
keeps its own message list locally and returns only findings + a summary.
That is context isolation, and it is what stops a four-agent run from
carrying four full transcripts into every subsequent step.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from atlas.domain.types import Finding, SpecialistResult


def merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Key-wise merge; right wins on conflict."""
    out = dict(left or {})
    out.update(right or {})
    return out


def take_last(left: Any, right: Any) -> Any:
    """Last-write-wins for fields with a single writer."""
    return right if right is not None else left


class DDState(TypedDict, total=False):
    """State flowing through the due-diligence graph."""

    # --- inputs (written once by the caller) ---
    repo: str
    repo_root: str
    requested_categories: list[str]
    # Unique per invocation. The cost ledger keys on this, not on `repo`:
    # two concurrent analyses of the same repository are trivially reachable
    # (two principals, one POST each) and keying by name pooled their
    # budgets, so one run's spend halted the other and one run's cleanup
    # erased the other's in-flight accounting.
    run_id: str

    # --- supervisor plan ---
    plan: Annotated[list[str], take_last]
    strategy: Annotated[str, take_last]
    memory_block: Annotated[str, take_last]

    # --- parallel specialist outputs (must accumulate) ---
    findings: Annotated[list[Finding], operator.add]
    results: Annotated[list[SpecialistResult], operator.add]
    summaries: Annotated[dict[str, str], merge_dicts]
    errors: Annotated[dict[str, str], merge_dicts]

    # --- run accounting (must sum) ---
    tokens_used: Annotated[int, operator.add]
    model_calls: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]
    steps: Annotated[int, operator.add]

    # --- synthesis ---
    verdict: Annotated[str, take_last]
