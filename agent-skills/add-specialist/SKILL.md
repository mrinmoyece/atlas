---
name: add-specialist
description: Add a new specialist agent (a new due-diligence dimension) to Atlas, with its A2A card, prompt, graph node, scripted scenarios and eval coverage. Use when asked to add an agent, dimension, or analysis category.
---

# Add a specialist to Atlas

## Procedure

1. **Add the category** to `Category` in `src/atlas/domain/types.py`.
2. **Write the brief** in `src/atlas/agents/prompts.py::_SPECIALIST_BRIEF`.
   It must state what to look for and how to rank. The shared injection
   guard, evidence rule and output contract are added automatically — do not
   duplicate them.
3. **Publish an agent card** in `src/atlas/a2a/cards.py` with capability
   tags. The supervisor routes by tag, so **do not** edit the graph to
   mention the new agent by name.
4. **Register the node**: the graph builds one node per category in
   `ALL_CATEGORIES`; add yours there.
5. **Script it** in `src/atlas/evals/scenarios.py` for both fixture repos,
   including a realistic miss. Route marker must match text that appears in
   your brief.
6. **Extend ground truth** in `evals/golden/ground_truth.yaml` — planted
   issues for the dirty repo, traps for the clean one.
7. **Re-baseline**: `make evals && make benchmark`. If gates move,
   justify it.

## Checklist
- [ ] card published with tags; graph untouched except `ALL_CATEGORIES`
- [ ] scripted for both repos, with at least one deliberate miss
- [ ] ground truth updated (must_find and should_not_find)
- [ ] `make lint && make test && make evals` pass
- [ ] benchmark/RESULTS.md and docs/MEMORY.md regenerated if numbers moved
