"""Model layer: a real-provider factory plus a deterministic scripted model."""

from atlas.llm.factory import build_model
from atlas.llm.scripted import ScriptedChatModel, ScriptedTurn, tool_call

__all__ = ["ScriptedChatModel", "ScriptedTurn", "tool_call", "build_model"]
