"""The journal: every card is stored, then scored against what actually happened.

This is the honesty mechanism. A model that sounds confident is worth nothing
until its calls are dated and graded. Scoring compares the card's invalidation
level and first target against subsequent prices, so a reviewer can measure the
app rather than trust it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

HORIZONS_DAYS = (30, 90, 180, 365)


@dataclass
class Entry:
    id: str
    created_at: str
    instrument: str
    mode: str
    verdict: str
    price_at_call: float
    invalidation: float
    first_target: float
    card: dict = field(default_factory=dict)
    outcomes: dict = field(default_factory=dict)   # horizon_days -> outcome dict
    settled: bool = False
    stop_policy: str = "close_only_no_gap_model"
    rules_version: str = ""
    rubric_version: str = ""

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True)


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[Entry]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    data = json.loads(line)
                    out.append(Entry(**data))
        return out

    def append(self, card: dict) -> Entry:
        """Store a card. Append-only: nothing is ever rewritten in place."""
        risk = card.get("risk") or {}
        standard = card.get("standard_lens") or {}
        price = standard.get("price")
        if price is None or not risk:
            raise ValueError("card must carry a price and risk levels before it can be journaled")
        existing = self._read()
        entry = Entry(
            id=f"j_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{len(existing) + 1}",
            created_at=card.get("created_at") or datetime.now(timezone.utc).isoformat(),
            instrument=card["instrument"],
            mode=card.get("mode", "swing"),
            verdict=card.get("verdict", ""),
            price_at_call=float(price),
            invalidation=float(risk["invalidation"]),
            first_target=float(risk["first_target"]),
            card=card,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(entry.to_json() + "\n")
        return entry

    def all(self) -> list[Entry]:
        return self._read()

    @staticmethod
    def _bar(row) -> dict:
        """Accept (date, close), (date, open, high, low, close) or a dict."""
        if isinstance(row, dict):
            return {
                "date": str(row["date"])[:10],
                "open": float(row.get("open", row.get("close"))),
                "high": float(row.get("high", row.get("close"))),
                "low": float(row.get("low", row.get("close"))),
                "close": float(row["close"]),
            }
        if len(row) == 2:
            d, c = row
            return {"date": str(d)[:10], "open": float(c), "high": float(c),
                    "low": float(c), "close": float(c)}
        d, o, h, l, c = row
        return {"date": str(d)[:10], "open": float(o), "high": float(h),
                "low": float(l), "close": float(c)}

    def score_entries(self, prices_by_instrument: dict[str, list],
                      adjusted: bool = False, source: str = "unspecified") -> list[Entry]:
        """Grade open entries against a price series per instrument.

        Each bar may be (date, close), (date, open, high, low, close) or a dict with those
        keys. When open/high/low are present the stop is simulated the way a stop order
        actually fills:

            open <= stop        -> filled at the open (the gap is the loss, not the stop)
            low  <= stop        -> filled at the stop
            otherwise           -> not stopped

        With close-only data the basis is recorded as `close_only`, which understates
        stop-outs on an intraday wick and overstates them on a gap — the label says so
        rather than pretending the number is exact.

        Also recorded: `corporate_action_suspected` when a bar drops more than 25% without
        touching the stop (an unadjusted split or a delisting, not a trading outcome), and
        `sparse` when the window holds far fewer bars than a trading calendar implies.
        """
        entries = self._read()
        scored: list[Entry] = []
        for e in entries:
            series = prices_by_instrument.get(e.instrument)
            if not series:
                scored.append(e)
                continue
            call_day = date.fromisoformat(e.created_at[:10])
            has_ohlc = any(isinstance(r, dict) or len(r) >= 5 for r in series)
            bars = [self._bar(r) for r in series]
            bars = [b for b in bars if date.fromisoformat(b["date"]) > call_day]
            if not bars:
                scored.append(e)
                continue

            for h in HORIZONS_DAYS:
                if f"{h}" in e.outcomes:
                    continue
                target = call_day.toordinal() + h
                window = [b for b in bars if date.fromisoformat(b["date"]).toordinal() <= target]
                if not window:
                    continue

                stopped = False
                exit_price = window[-1]["close"]
                exit_day = window[-1]["date"]
                basis = "not_stopped"
                for bar in window:
                    if bar["low"] <= e.invalidation:
                        stopped = True
                        if bar["open"] <= e.invalidation:
                            exit_price, basis = bar["open"], "gap_open"
                        else:
                            exit_price, basis = e.invalidation, "intrabar_stop"
                        exit_day = bar["date"]
                        break

                suspected = None
                prev = None
                for bar in window:
                    if prev and bar["close"] / prev - 1 < -0.25 and bar["low"] > e.invalidation:
                        suspected = f"drop >25% on {bar['date']} without touching the stop"
                        break
                    prev = bar["close"]

                expected = max(1, int(h * 5 / 7) - 1)
                e.outcomes[str(h)] = {
                    "horizon_days": h,
                    "price": round(window[-1]["close"], 4),
                    "exit_price": round(exit_price, 4),
                    "exit_date": exit_day,
                    "return": round(exit_price / e.price_at_call - 1, 4),
                    "return_hold": round(window[-1]["close"] / e.price_at_call - 1, 4),
                    "hit_target": any(b["high"] >= e.first_target for b in window),
                    "stopped": stopped,
                    "stopped_basis": basis if has_ohlc else "close_only",
                    "stop_policy": "gap_adjusted_stop" if has_ohlc else "close_only_no_gap_model",
                    "bars": len(window),
                    "sparse": len(window) < expected * 0.8,
                    "adjusted_for_splits": adjusted,
                    "price_source": source,
                    "corporate_action_suspected": suspected,
                }
            if all(str(h) in e.outcomes for h in HORIZONS_DAYS):
                e.settled = True
            scored.append(e)

        # rewrite whole file atomically: this is a materialisation of derived fields,
        # never an edit of the original call itself.
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for e in scored:
                fh.write(e.to_json() + "\n")
        tmp.replace(self.path)
        return scored


def summary(entries: Iterable[Entry]) -> dict:
    """Aggregate outcomes — including the losers, which is the point."""
    rows = list(entries)
    out: dict[str, dict] = {}
    for h in HORIZONS_DAYS:
        graded = [e for e in rows if str(h) in e.outcomes]
        if not graded:
            continue
        rets = [e.outcomes[str(h)]["return"] for e in graded]
        out[str(h)] = {
            "n": len(graded),
            "corporate_action_flags": sum(
                1 for e in graded if e.outcomes[str(h)].get("corporate_action_suspected")
            ),
            "gap_fills": sum(
                1 for e in graded if e.outcomes[str(h)].get("stopped_basis") == "gap_open"
            ),
            "avg_return": round(sum(rets) / len(rets), 4),
            "hit_target_rate": round(sum(1 for e in graded if e.outcomes[str(h)]["hit_target"]) / len(graded), 4),
            "stopped_rate": round(sum(1 for e in graded if e.outcomes[str(h)]["stopped"]) / len(graded), 4),
            "worst": round(min(rets), 4),
            "best": round(max(rets), 4),
        }
    return out
