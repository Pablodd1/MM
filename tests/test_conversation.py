"""The crowd talks. These tests pin the behaviour that was measured, not the behaviour assumed."""

from __future__ import annotations

import pytest

from mmdesk import conversation as C


def crowd(values: list[float], conviction: float = 0.6) -> list[C.Agent]:
    return [C.Agent(f"a{i}", "m", v, conviction) for i, v in enumerate(values)]


def test_larger_tolerance_means_less_tolerance():
    """Guard against the parameter being flipped: a huge tolerance must average everyone."""
    out = C.run(crowd([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]), rounds=30, topology="clique",
                trust=0.6, tolerance=2.0)
    assert out["polarised"] is False
    assert out["spread"] < 0.05


def test_bounded_confidence_polarises_into_camps():
    out = C.run(crowd([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]), rounds=30, topology="clique",
                trust=0.6, tolerance=0.5)
    assert out["polarised"] is True
    assert out["spread"] == pytest.approx(2.0, abs=0.01)
    assert out["counts"] == {"add": 3, "hold": 0, "trim": 3}


def test_averaging_never_keeps_a_balanced_crowd_apart():
    """The finding that made bounded confidence necessary: DeGroot always converges.

    It shrinks the gap monotonically — never widens it — so a balanced crowd cannot stay
    split. A single dissenter survives at low trust (see the majority test) and vanishes at
    high trust; neither is polarisation.
    """
    for trust in (0.1, 0.5, 1.0, 1.5):
        start = C.run(crowd([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]), rounds=0, topology="clique",
                      trust=trust, model="averaging")
        out = C.run(crowd([-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]), rounds=40, topology="clique",
                    trust=trust, model="averaging")
        assert out["spread"] < start["spread"], f"trust {trust} failed to converge"
        assert out["spread"] < 0.3, f"trust {trust} left the crowd materially split"


def test_consensus_preserves_the_majority_direction():
    out = C.run(crowd([-0.6] * 5 + [0.9]), rounds=40, topology="clique", trust=0.25,
                model="averaging")
    assert all(v < 0 for v in out["final_values"]), "a lone dissenter must not flip the crowd"


def test_traces_have_one_point_per_round():
    out = C.run(crowd([0.2, -0.2]), rounds=7, topology="ring")
    for trace in out["traces"].values():
        assert len(trace) == 8


def test_unknown_topology_and_model_are_rejected():
    with pytest.raises(ValueError):
        C.run(crowd([0.0, 0.1]), rounds=1, topology="nope")
    with pytest.raises(ValueError):
        C.run(crowd([0.0, 0.1]), rounds=1, model="nope")


def test_values_stay_in_range():
    out = C.run(crowd([1.0, -1.0, 1.0, -1.0]), rounds=50, topology="ring", trust=2.0)
    assert all(-1.0 <= v <= 1.0 for v in out["final_values"])


def test_starter_crowd_runs_without_a_language_model():
    out = C.run(C.starter_crowd(), rounds=10, topology="ring")
    assert out["agents"] == 6
    assert out["model"] == "bounded_confidence"
