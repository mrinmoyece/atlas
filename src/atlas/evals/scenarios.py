"""Scripted model behaviour for the fixture repositories.

This file is the "simulated model" for the whole evaluation stack. Each
route describes how a specialist behaves on a given repo, per pattern
phase, so a full four-agent run is deterministic end to end.

The scripts are written to be *realistic, not flattering*:

  * the ReAct/default script for `legacy-billing` finds most planted issues
    but not all - real agents miss things,
  * ReWOO's script finds fewer, because committing to a plan before seeing
    evidence genuinely loses leads,
  * Reflexion's revision drops its weakest finding, trading recall for
    precision,
  * one specialist deliberately produces a **trap** finding on the clean
    repo, so the false-positive machinery is exercised rather than
    theoretical.

If every script found everything, the benchmark and the eval suite would
prove nothing.
"""

from __future__ import annotations

import json
from typing import Any

from atlas.llm.scripted import ScriptedChatModel, ScriptedTurn, tool_call

# Markers that appear in the specialist system prompts (agents/prompts.py)
SECURITY = "security specialist"
ARCHITECTURE = "architecture specialist"
DEPENDENCY = "dependency specialist"
DELIVERY = "delivery maturity specialist"

# Markers that identify a *phase* (from the pattern instruction text)
REWOO_PLAN = "plan your entire evidence-gathering"
# Phrase unique to ReWOO's solver turn (not shared with plan-execute's
# synthesis turn, which says "worked through the plan").
REWOO_SOLVE = "results of every tool call you planned"
PLANEX_PLAN = "write a short plan"
REFLEX_CRITIQUE = "criticise your own draft"
REFLEX_REVISE = "apply your critique"

# Marker present only when MemoryHub injected prior experience into the
# system prompt. Routing on it lets the scripted model be *memory-sensitive*,
# which is what allows the A/B harness to be validated end to end. See the
# honesty note in docs/MEMORY.md: this simulates a memory benefit so the
# measurement machinery can be exercised; it is not evidence that memory
# helps a live model, which must be re-measured against a real provider.
MEMORY_PRIMED = "prior_experience"


def _final(summary: str, findings: list[dict[str, Any]]) -> ScriptedTurn:
    return ScriptedTurn(
        content="Analysis complete.\n```json\n"
        + json.dumps({"summary": summary, "findings": findings}, indent=2)
        + "\n```"
    )


def _f(
    rule: str,
    title: str,
    severity: str,
    path: str,
    line: int,
    detail: str = "",
    remediation: str = "Refactor the highlighted code to remove the issue.",
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "rule": rule,
        "title": title,
        "severity": severity,
        "confidence": confidence,
        "detail": detail or title,
        "path": path,
        "line": line,
        "remediation": remediation,
    }


# --------------------------------------------------------------------------
# legacy-billing - the dirty repo
# --------------------------------------------------------------------------

_LEGACY_SECURITY_FINDINGS = [
    _f(
        "sql_injection",
        "SQL built by string concatenation",
        "critical",
        "src/db.py",
        7,
        "invoice_id is concatenated directly into the query",
        "Use parameterised queries with placeholders.",
    ),
    _f(
        "sql_injection",
        "SQL built with % formatting",
        "high",
        "src/db.py",
        12,
        "report name interpolated via % into SQL",
        "Use parameterised queries with placeholders.",
    ),
    _f(
        "hardcoded_secret",
        "Database password committed to source",
        "high",
        "src/db.py",
        3,
        "DB_PASSWORD literal in module scope",
        "Move to a secret manager and rotate the exposed credential.",
    ),
    _f(
        "hardcoded_secret",
        "Live API key committed to source",
        "critical",
        "src/api.py",
        8,
        "API_KEY literal appears to be a live key",
        "Revoke the key immediately and load from environment.",
    ),
    _f(
        "command_injection",
        "Shell command from user input",
        "critical",
        "src/api.py",
        22,
        "os.popen executes an attacker-controlled query parameter",
        "Remove the endpoint; never pass user input to a shell.",
    ),
    _f(
        "unsafe_deserialization",
        "pickle.loads on request body",
        "high",
        "src/api.py",
        14,
        "pickle deserialisation of untrusted input is remote code execution",
        "Use JSON and validate against a schema.",
    ),
]

# Missing authentication is the classic finding a first-pass review skips:
# nothing in the code *looks* wrong, the flaw is what isn't there. The base
# script therefore misses it, and the memory-primed script catches it -
# which is what the A/B harness measures.
_MISSING_AUTH = _f(
    "missing_authentication",
    "Money-moving endpoint has no auth",
    "high",
    "src/api.py",
    12,
    "/charge accepts requests with no authentication or authorisation",
    "Require an authenticated principal and authorise the action.",
)

_LEGACY_ARCH_FINDINGS = [
    _f(
        "missing_resilience",
        "External call without timeout or breaker",
        "medium",
        "src/api.py",
        17,
        "requests.post has no timeout, retry or circuit breaker",
        "Set an explicit timeout and wrap in a circuit breaker.",
    ),
]

_LEGACY_DEP_FINDINGS = [
    _f(
        "vulnerable_dependency",
        "Known-vulnerable pinned versions",
        "high",
        "requirements.txt",
        3,
        "pyyaml 3.13 and django 1.11 carry published CVEs",
        "Upgrade to supported releases and add automated scanning.",
    ),
    _f(
        "unpinned_dependency",
        "Unpinned dependencies",
        "medium",
        "requirements.txt",
        1,
        "flask and requests float to any version",
        "Pin exact versions and use a lockfile.",
    ),
]

_LEGACY_DELIVERY_FINDINGS = [
    _f(
        "no_ci",
        "No continuous integration",
        "medium",
        ".github",
        1,
        "no workflow definitions found",
        "Add a CI pipeline running tests on every push.",
    ),
    _f(
        "no_tests",
        "No automated tests",
        "high",
        ".",
        1,
        "no test files found anywhere in the repository",
        "Introduce a test suite starting with the billing paths.",
    ),
]


def _legacy_routes() -> dict[str, list[ScriptedTurn]]:
    return {
        # --- security ---
        SECURITY: [
            ScriptedTurn(
                tool_calls=(
                    tool_call("grep", {"pattern": "SELECT|execute|popen|pickle", "glob": "*.py"}),
                ),
            ),
            ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "src/api.py"}),)),
            _final(
                "Multiple critical, directly exploitable weaknesses in a service that "
                "moves money. Not safe to operate as-is.",
                _LEGACY_SECURITY_FINDINGS,
            ),
        ],
        # Memory-primed: prior experience reminded the agent to check for
        # absent authorisation, which the cold run missed.
        f"{SECURITY} && {MEMORY_PRIMED}": [
            ScriptedTurn(
                tool_calls=(
                    tool_call("grep", {"pattern": "SELECT|execute|popen|pickle", "glob": "*.py"}),
                ),
            ),
            ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "src/api.py"}),)),
            _final(
                "Multiple critical weaknesses, including an unauthenticated "
                "money-moving endpoint flagged from prior experience.",
                [*_LEGACY_SECURITY_FINDINGS, _MISSING_AUTH],
            ),
        ],
        f"{SECURITY} && {REWOO_PLAN}": [
            ScriptedTurn(
                content=json.dumps(
                    {
                        "steps": [
                            {"tool": "grep", "args": {"pattern": "SELECT|execute", "glob": "*.py"}},
                            {"tool": "read_file", "args": {"path": "src/db.py"}},
                        ]
                    }
                ),
            )
        ],
        # ReWOO committed to a plan before seeing evidence: it never opened
        # api.py, so it can only report what db.py revealed. This is the
        # concrete cost of not being able to follow a lead mid-run.
        f"{SECURITY} && {REWOO_SOLVE}": [
            _final(
                "Reviewed the files in my plan. SQL construction in db.py is unsafe.",
                [f for f in _LEGACY_SECURITY_FINDINGS if f["path"] == "src/db.py"],
            )
        ],
        f"{SECURITY} && {PLANEX_PLAN}": [
            ScriptedTurn(
                content=json.dumps(
                    {"plan": ["Scan for injection sinks", "Scan for hardcoded credentials"]}
                ),
            )
        ],
        f"{SECURITY} && {REFLEX_CRITIQUE}": [
            ScriptedTurn(
                content=(
                    "Reviewing my own draft: the two SQL findings are the same root "
                    "cause in one file and should not be double-counted at different "
                    "severities. Everything else cites a concrete line and stands."
                ),
            )
        ],
        f"{SECURITY} && {REFLEX_REVISE}": [
            _final(
                "Critical exploitable weaknesses confirmed after self-review; "
                "duplicate SQL finding merged.",
                [f for f in _LEGACY_SECURITY_FINDINGS if f["line"] != 12],
            )
        ],
        # --- architecture ---
        ARCHITECTURE: [
            ScriptedTurn(
                tool_calls=(tool_call("grep", {"pattern": "requests\\.|httpx", "glob": "*.py"}),),
            ),
            _final(
                "Outbound payment call has no resilience controls; a downstream "
                "slowdown will exhaust workers.",
                _LEGACY_ARCH_FINDINGS,
            ),
        ],
        # --- dependency ---
        DEPENDENCY: [
            ScriptedTurn(tool_calls=(tool_call("dependency_manifest", {}),)),
            _final(
                "Dependency set mixes unpinned packages with known-vulnerable pins.",
                _LEGACY_DEP_FINDINGS,
            ),
        ],
        # --- delivery ---
        DELIVERY: [
            ScriptedTurn(tool_calls=(tool_call("repo_stats", {}),)),
            _final(
                "No CI, no tests, no container: ownership cost will be high.",
                _LEGACY_DELIVERY_FINDINGS,
            ),
        ],
    }


# --------------------------------------------------------------------------
# modern-payments - the clean repo (false-positive control)
# --------------------------------------------------------------------------


def _modern_routes() -> dict[str, list[ScriptedTurn]]:
    return {
        SECURITY: [
            ScriptedTurn(
                tool_calls=(tool_call("grep", {"pattern": "execute|SELECT", "glob": "*.py"}),),
            ),
            _final(
                "Queries are parameterised and no credentials are embedded. No security findings.",
                [],
            ),
        ],
        ARCHITECTURE: [
            ScriptedTurn(tool_calls=(tool_call("read_file", {"path": "src/client.py"}),)),
            # DELIBERATE TRAP: flags a timeout that is actually configured.
            # This is here so the false-positive and trap machinery is
            # exercised by the default eval run, not just in theory.
            _final(
                "Client looks reasonable, though I would double-check resilience.",
                [
                    _f(
                        "missing_resilience",
                        "Possible missing retry policy on payments client",
                        "medium",
                        "src/client.py",
                        9,
                        "no retry decorator visible on charge()",
                        confidence="low",
                    )
                ],
            ),
        ],
        DEPENDENCY: [
            ScriptedTurn(tool_calls=(tool_call("dependency_manifest", {}),)),
            _final("Dependencies are pinned to current versions.", []),
        ],
        DELIVERY: [
            ScriptedTurn(tool_calls=(tool_call("repo_stats", {}),)),
            _final("CI, tests, README and Dockerfile all present.", []),
        ],
        f"{ARCHITECTURE} && {REFLEX_CRITIQUE}": [
            ScriptedTurn(
                content=(
                    "On review, client.py sets an explicit timeout and retries are a "
                    "caller-side policy. My draft finding is speculative and should be "
                    "dropped."
                )
            )
        ],
        # Reflexion correctly removes the trap - which is exactly why its
        # precision beats ReAct's in the benchmark.
        f"{ARCHITECTURE} && {REFLEX_REVISE}": [
            _final("No architecture findings after self-review.", [])
        ],
    }


REPO_SCENARIOS: dict[str, dict[str, list[ScriptedTurn]]] = {
    "legacy-billing": _legacy_routes(),
    "modern-payments": _modern_routes(),
}


def model_for(repo: str, *, strict: bool = True) -> ScriptedChatModel:
    """A fresh deterministic model scripted for one fixture repository.

    `strict=True` by default, and that matters more than it looks. Without
    it, a prompt that matches no scripted route silently falls back to the
    default route - so a scenario with a missing or misnamed route still
    produces plausible output, and the eval numbers are measuring the
    fallback rather than the thing under test. That is precisely how the
    revision-vs-critique routing bug hid: recall halved and every test
    stayed green.

    `ScriptedChatModel` keeps the permissive mode because the demo and the
    docs benefit from never hard-failing mid-narrative; the evaluation path,
    where a wrong answer is worse than no answer, opts into strictness.
    """
    routes = REPO_SCENARIOS.get(repo)
    if routes is None:
        raise KeyError(f"no scenario for repo {repo!r}; have {sorted(REPO_SCENARIOS)}")
    return ScriptedChatModel(routes={k: list(v) for k, v in routes.items()}, strict_routes=strict)


def fixture_repos() -> list[str]:
    return sorted(REPO_SCENARIOS)
