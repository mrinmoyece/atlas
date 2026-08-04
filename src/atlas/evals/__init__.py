"""Evaluation: ground-truth scoring, judges, and tiered gates.

The 2026 survey finding that motivated this package: ~89% of teams running
agents in production have observability, but only ~52% have evals. That gap
is where quality dies, because observability tells you *what happened* and
only evals tell you whether it was *right*.
"""

from atlas.evals.judge import DeterministicJudge, JudgeScore, hallucination_rate
from atlas.evals.runner import GATES, EvalRun, evaluate, run_repo
from atlas.evals.scoring import ScoreCard, aggregate, load_ground_truth, score_report

__all__ = [
    "DeterministicJudge",
    "JudgeScore",
    "hallucination_rate",
    "GATES",
    "EvalRun",
    "evaluate",
    "run_repo",
    "ScoreCard",
    "aggregate",
    "load_ground_truth",
    "score_report",
]
