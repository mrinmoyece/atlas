"""A2A agent cards - machine-readable capability advertisements.

MCP answers "how does an agent talk to a tool". A2A answers "how does an
agent find and talk to another agent". The unit of discovery is the *agent
card*: a JSON document describing what an agent can do, what it accepts and
returns, and how to authenticate to it.

Why this matters architecturally, and why Atlas uses cards internally even
though all four specialists live in one process: the supervisor routes work
by *reading capabilities*, not by hard-coding a list of agents. That single
indirection is what lets a specialist be moved out of process, replaced by
a third-party agent, or added at runtime without touching the orchestrator.
Hard-coding the roster is the most common reason multi-agent systems cannot
be extended later.

Security note: an external agent's card is untrusted self-description, the
same threat model as an MCP tool description. `AgentRegistry.register`
therefore validates and caps card fields before they can reach a prompt.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas.domain.types import Category
from atlas.mcp_layer.client import sanitise_description

A2A_VERSION = "0.2"
MAX_SKILL_DESCRIPTION = 400


class Capability(BaseModel):
    """One advertised skill."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    input_modes: tuple[str, ...] = ("text",)
    output_modes: tuple[str, ...] = ("text", "application/json")


class AgentCard(BaseModel):
    """An agent's public description, in A2A card shape."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    version: str = "0.1.0"
    url: str = "local://in-process"
    protocol_version: str = A2A_VERSION
    capabilities: tuple[Capability, ...] = ()
    # Declared, not enforced here - enforcement belongs to the transport.
    authentication: dict[str, Any] = Field(default_factory=lambda: {"schemes": ["none"]})
    streaming: bool = True

    def matches(self, tags: Iterable[str]) -> bool:
        wanted = {t.lower() for t in tags}
        mine = {t.lower() for cap in self.capabilities for t in cap.tags}
        return bool(wanted & mine)

    def to_json(self) -> str:
        """The wire format an A2A peer would fetch from /.well-known/agent.json"""
        return json.dumps(self.model_dump(), indent=2)


class AgentRegistry:
    """Discovery: find agents by capability tag, not by name."""

    def __init__(self, cards: Sequence[AgentCard] = ()) -> None:
        self._cards: dict[str, AgentCard] = {}
        for card in cards:
            self.register(card)

    def register(self, card: AgentCard, *, trusted: bool = True) -> AgentCard:
        """Register a card. Untrusted (remote) cards are sanitised first."""
        if not trusted:
            card = _sanitise_card(card)
        self._cards[card.name] = card
        return card

    def get(self, name: str) -> AgentCard | None:
        return self._cards.get(name)

    def find(self, *tags: str) -> list[AgentCard]:
        return [card for card in self._cards.values() if card.matches(tags)]

    def names(self) -> list[str]:
        return sorted(self._cards)

    def routing_table(self) -> dict[str, list[str]]:
        """tag -> agents. This is what the supervisor consults to plan
        delegation, instead of an if/elif chain over agent names."""
        table: dict[str, list[str]] = {}
        for card in self._cards.values():
            for cap in card.capabilities:
                for tag in cap.tags:
                    table.setdefault(tag.lower(), []).append(card.name)
        return {k: sorted(v) for k, v in sorted(table.items())}

    def write_well_known(self, directory: Path) -> list[Path]:
        """Emit cards as /.well-known/agent.json style files - the thing a
        real A2A peer would fetch."""
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        for name, card in self._cards.items():
            path = directory / f"{name}.agent.json"
            path.write_text(card.to_json())
            written.append(path)
        return written


def _sanitise_card(card: AgentCard) -> AgentCard:
    clean_desc, _ = sanitise_description(card.description)
    caps = []
    for cap in card.capabilities:
        cap_desc, _ = sanitise_description(cap.description)
        caps.append(cap.model_copy(update={"description": cap_desc[:MAX_SKILL_DESCRIPTION]}))
    return card.model_copy(update={"description": clean_desc, "capabilities": tuple(caps)})


# --------------------------------------------------------------------------
# Atlas's own agents, described as cards
# --------------------------------------------------------------------------

_SPECIALIST_CARDS: dict[Category, AgentCard] = {
    Category.SECURITY: AgentCard(
        name="security-specialist",
        description="Finds exploitable weaknesses in source code with file/line evidence.",
        capabilities=(
            Capability(
                id="scan-source",
                name="Source security review",
                description="Injection, hardcoded secrets, unsafe deserialisation, weak crypto.",
                tags=("security", "sast", "risk"),
            ),
        ),
    ),
    Category.ARCHITECTURE: AgentCard(
        name="architecture-specialist",
        description="Assesses structure, coupling, and resilience patterns.",
        capabilities=(
            Capability(
                id="assess-structure",
                name="Architecture review",
                description="Layering violations, missing resilience, God objects, coupling.",
                tags=("architecture", "design", "risk"),
            ),
        ),
    ),
    Category.DEPENDENCY: AgentCard(
        name="dependency-specialist",
        description="Assesses third-party dependency and supply-chain risk.",
        capabilities=(
            Capability(
                id="assess-dependencies",
                name="Dependency risk review",
                description="Unpinned versions, abandoned packages, licence and CVE exposure.",
                tags=("dependency", "supply-chain", "risk"),
            ),
        ),
    ),
    Category.DELIVERY: AgentCard(
        name="delivery-specialist",
        description="Assesses engineering maturity signals: CI, tests, docs, release hygiene.",
        capabilities=(
            Capability(
                id="assess-delivery",
                name="Delivery maturity review",
                description="CI presence, test coverage signals, documentation, release process.",
                tags=("delivery", "process", "maturity"),
            ),
        ),
    ),
}


def supervisor_card() -> AgentCard:
    return AgentCard(
        name="dd-supervisor",
        description="Plans a due-diligence review and delegates to specialists by capability.",
        capabilities=(
            Capability(
                id="due-diligence",
                name="Technical due diligence",
                description="Produces an evidence-backed report on a repository.",
                tags=("due-diligence", "orchestration"),
            ),
        ),
    )


def atlas_agent_cards() -> list[AgentCard]:
    return [supervisor_card(), *_SPECIALIST_CARDS.values()]


def card_for(category: Category) -> AgentCard:
    return _SPECIALIST_CARDS[category]
