"""MM Desk — decision-support core."""

from .card import Card, Citation
from .decision import build_decision_card, reconcile
from .rules import Account, Setup, exit_signals, position_pct, score_10, size_position
from .standard import Bars, standard_lens

__all__ = [
    "Account", "Bars", "Card", "Citation", "Setup",
    "build_decision_card", "exit_signals", "position_pct", "reconcile",
    "score_10", "size_position", "standard_lens",
]
__version__ = "0.1.0"
