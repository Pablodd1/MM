"""Persona simulation that mirrors social feedback — without a language model per agent.

The difference between this and a pile of independent rules: agents **talk to each other**.
Each round, an agent reads its neighbours' current stances and may move toward them, weighted
by how much it trusts its neighbours and how strongly it holds its own view. That is the
mechanism MiroFish-style engines try to capture — social influence — and it can be run with
arithmetic instead of tokens.

What it simulates: how a crowd's opinion drifts when opinions are contagious.
What it does not simulate: conversation, argument, new information, or prices.

**Measured behaviour.** DeGroot-style linear averaging (each agent moves toward the mean of its
neighbours) converges to consensus whenever the pull is at most the whole gap — that is, when
``trust * (1 - conviction) <= 1``, which holds for every trust weight the API accepts unless an
agent's conviction is near zero. Above that the update overshoots and oscillates instead.

Bounded confidence is different: opinions more than ``tolerance`` apart are rejected rather than
averaged, so a crowd that starts apart stays apart. A larger ``tolerance`` makes agents *more*
open. Neither model can split a crowd that starts together. Both are here, and the difference is
reported rather than claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


def _label(value: float) -> str:
    if value >= 0.33:
        return "add"
    if value <= -0.33:
        return "trim"
    return "hold"


MODELS = ("averaging", "bounded_confidence")


@dataclass
class Agent:
    name: str
    mandate: str
    value: float          # -1 trim .. +1 add
    conviction: float     # 0..1 — resistance to influence
    trust: float = 0.25   # how much a neighbour's view moves this agent
    path: list[float] = field(default_factory=list)


def _network(agents: list[Agent], topology: str) -> list[list[int]]:
    n = len(agents)
    if topology == "clique":
        return [[j for j in range(n) if j != i] for i in range(n)]
    if topology == "ring":
        return [[(i - 1) % n, (i + 1) % n] for i in range(n)]
    if topology == "two_camps":
        half = n // 2
        return [[j for j in range(n) if (j < half) == (i < half) and j != i] for i in range(n)]
    raise ValueError(f"unknown topology: {topology}")


def run(agents: list[Agent], rounds: int, topology: str = "ring",
        trust: float = 0.25, model: str = "bounded_confidence",
        tolerance: float = 0.5) -> dict:
    """One round = everyone reads their neighbours, then everyone updates simultaneously.

    `model="averaging"` is DeGroot consensus. `model="bounded_confidence"` ignores neighbours
    whose position is more than `tolerance` away — the rule that lets a crowd stay split.

    The agents passed in are left as they were; the simulation works on copies.
    """
    if model not in MODELS:
        raise ValueError(f"unknown model: {model}")
    agents = [replace(a, trust=trust, path=[a.value]) for a in agents]
    neighbours = _network(agents, topology)
    for _ in range(rounds):
        new_values = []
        for i, agent in enumerate(agents):
            peers = neighbours[i]
            if not peers:
                new_values.append(agent.value)
                continue
            if model == "bounded_confidence":
                peers = [j for j in peers if abs(agents[j].value - agent.value) <= tolerance]
                if not peers:
                    new_values.append(agent.value)
                    continue
            peer_mean = sum(agents[j].value for j in peers) / len(peers)
            pull = agent.trust * (1 - agent.conviction) * (peer_mean - agent.value)
            new_values.append(max(-1.0, min(1.0, agent.value + pull)))
        for agent, value in zip(agents, new_values, strict=True):
            agent.value = value
            agent.path.append(value)

    final = [round(a.value, 3) for a in agents]
    counts = {"add": 0, "hold": 0, "trim": 0}
    for v in final:
        counts[_label(v)] += 1
    spread = max(final) - min(final) if final else 0.0
    # Polarised if both extremes are populated after the rounds.
    polarised = any(v >= 0.5 for v in final) and any(v <= -0.5 for v in final)
    return {
        "model": model,
        "tolerance": tolerance,
        "topology": topology,
        "rounds": rounds,
        "trust": trust,
        "final_values": final,
        "counts": counts,
        "spread": round(spread, 3),
        "polarised": polarised,
        "agents": len(agents),
        "traces": {a.name: [round(x, 3) for x in a.path] for a in agents},
    }


def starter_crowd() -> list[Agent]:
    """The same six styles as `mmdesk.scenarios`, expressed as continuous positions."""
    return [
        Agent("Value investor", "buy below fair value", 0.5, 0.6),
        Agent("Momentum trader", "buy strength", -0.6, 0.5),
        Agent("Risk manager", "survive first", -0.8, 0.8),
        Agent("Cash holder", "get paid to wait", 0.0, 0.5),
        Agent("Growth buyer", "pay for expansion", 0.4, 0.4),
        Agent("Contrarian", "buy fear", 0.2, 0.3),
    ]
