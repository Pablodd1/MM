"""The journal: every card is stored, then scored against what actually happened.

This is the honesty mechanism. A model that sounds confident is worth nothing
until its calls are dated and graded. Scoring compares the card's invalidation
level and first target against subsequent prices, so a reviewer can measure the
app rather than trust it.

Four rules protect the record:

* An outcome is written only once its horizon has closed. Grading an entry the day after it was
  made must not freeze a one-day move as the 365-day outcome.
* Outcomes are persisted only by `/api/journal/grade`, which fetches the prices itself. The
  endpoint that accepts caller-supplied prices never writes.
* The window starts after the bar whose close was the call price, not after the call's calendar
  date — an evening or pre-market call must not lose the next session.
* Stored levels are rescaled when the fetched series has been adjusted since the call, so a
  split becomes a rescaling and not a fabricated stop-out.

A damaged line cannot take the journal down: it is set aside, counted, and reported.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from pathlib import Path

HORIZONS_DAYS = (30, 90, 180, 365)
UNFETCHED = "unspecified"   # price_source of an outcome no provider produced
# A reference close this far from the recorded price means the series was adjusted (split or
# dividend), not that the call was made a little off the close.
ADJUSTMENT_TOLERANCE = 0.005


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
    price_bar_date: str = ""      # date of the bar whose close was used as the call price

    def to_json(self) -> str:
        # allow_nan=False: a non-finite number must fail here, not be written as the token
        # `Infinity`, which is not JSON and poisons every later summary.
        return json.dumps(self.__dict__, sort_keys=True, allow_nan=False)


_FIELDS = tuple(f.name for f in fields(Entry))
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
    return lock


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _finite(value: object, what: str) -> float:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{what} is not a number") from None
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"{what} must be a finite positive number")
    return x


def _date(value: object) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not an ISO date") from None


class Journal:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.damaged = 0          # lines that could not be read, as of the last read
        self._damaged_lines: list[str] = []

    @staticmethod
    def _entry(data: object) -> Entry:
        if not isinstance(data, dict):
            raise ValueError("entry is not an object")
        for key in ("price_at_call", "invalidation", "first_target"):
            if key in data:
                data[key] = _finite(data[key], key)
        return Entry(**{k: v for k, v in data.items() if k in _FIELDS})

    def _read(self) -> list[Entry]:
        """Parse the file. A line that will not parse is counted, kept aside, and skipped.

        One truncated line used to raise on every read — and because `append` reads too, a single
        bad line stopped the tenant from analysing anything.
        """
        self.damaged = 0
        self._damaged_lines = []
        if not self.path.exists():
            return []
        out: list[Entry] = []
        with self.path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    out.append(self._entry(json.loads(line, parse_constant=_reject_constant)))
                except (TypeError, ValueError, KeyError):
                    self.damaged += 1
                    self._damaged_lines.append(raw if raw.endswith("\n") else raw + "\n")
        return out

    def append(self, card: dict) -> Entry:
        """Store a card. Append-only: nothing is ever rewritten in place."""
        risk = card.get("risk") or {}
        standard = card.get("standard_lens") or {}
        price = standard.get("price")
        if price is None or not risk:
            raise ValueError("card must carry a price and risk levels before it can be journaled")
        with _lock_for(self.path):
            # The id carries a random suffix rather than a line count: counting the lines meant
            # parsing the whole journal on every call.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
            entry = Entry(
                id=f"j_{stamp}_{uuid.uuid4().hex[:6]}",
                created_at=card.get("created_at") or datetime.now(timezone.utc).isoformat(),
                instrument=card["instrument"],
                mode=card.get("mode", "swing"),
                verdict=card.get("verdict", ""),
                price_at_call=float(price),
                invalidation=float(risk["invalidation"]),
                first_target=float(risk["first_target"]),
                card=card,
                price_bar_date=str((card.get("grounding") or {}).get("price_bar") or ""),
            )
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json() + "\n")
        return entry

    def all(self) -> list[Entry]:
        with _lock_for(self.path):
            return self._read()

    @staticmethod
    def _bar(row: object) -> dict:
        """Accept (date, close), (date, open, high, low, close) or a dict with those keys."""
        if isinstance(row, dict):
            has_ohlc = any(row.get(k) is not None for k in ("open", "high", "low"))
            close = _finite(row.get("close"), "close")
            out = {"date": row.get("date"), "close": close}
            if has_ohlc:
                out["open"] = _finite(row.get("open", close), "open")
                out["high"] = _finite(row.get("high", close), "high")
                out["low"] = _finite(row.get("low", close), "low")
        elif isinstance(row, (list, tuple)):
            if len(row) == 2:
                has_ohlc = False
                close = _finite(row[1], "close")
                out = {"date": row[0], "close": close}
            elif len(row) == 5:
                has_ohlc = True
                out = {"date": row[0], "open": _finite(row[1], "open"),
                       "high": _finite(row[2], "high"), "low": _finite(row[3], "low"),
                       "close": _finite(row[4], "close")}
            else:
                raise ValueError("a bar is [date, close] or [date, open, high, low, close]")
        else:
            raise ValueError("each bar must be a list or an object")
        if not has_ohlc:
            out.update(open=out["close"], high=out["close"], low=out["close"])
        out["ohlc"] = has_ohlc
        return out

    @classmethod
    def _bars(cls, series: list, instrument: str) -> list[tuple[date, dict]]:
        """Parse, sort by date and keep one bar per date (the last one supplied)."""
        by_day: dict[date, dict] = {}
        for i, row in enumerate(series):
            try:
                bar = cls._bar(row)
                day = _date(bar["date"])
            except ValueError as exc:
                raise ValueError(f"{instrument} bar {i}: {exc}") from None
            by_day[day] = bar | {"date": day.isoformat()}
        return sorted(by_day.items())

    @staticmethod
    def _reference(e: Entry, bars: list[tuple[date, dict]],
                   call_day: date) -> tuple[date, float | None]:
        """The date of the bar whose close was the call price, and that close if it is known.

        New entries record the date. For entries written before that, the latest bar at or
        before the call whose close matches the recorded price is the reference; failing that,
        the last bar before the call. With no bar at all before the call the call date itself is
        the reference and the close is unknown.
        """
        if e.price_bar_date:
            wanted = e.price_bar_date[:10]
            for day, bar in bars:
                if day.isoformat() == wanted:
                    return day, bar["close"]
        upto = [(day, bar) for day, bar in bars if day <= call_day]
        for day, bar in reversed(upto):
            if abs(bar["close"] / e.price_at_call - 1) <= 0.001:
                return day, bar["close"]
        return (upto[-1][0], upto[-1][1]["close"]) if upto else (call_day, None)

    def score_entries(self, prices_by_instrument: dict[str, list],
                      adjusted: bool = False, source: str = UNFETCHED, persist: bool = True,
                      today: date | None = None) -> list[Entry]:
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

        A horizon is graded only once its window has closed: the series runs past the horizon
        date, or that date is behind us. Before then the outcome is simply absent — never a
        partial window frozen in place.

        If the fetched series has been adjusted since the call (a split, a dividend), the
        stored stop, target and entry are rescaled by the same factor and the outcome records
        `levels_adjusted_by`.

        Also recorded: `corporate_action_suspected` when a bar moves more than 25% in one
        session without touching the stop, and `sparse` when the window holds far fewer bars
        than a trading calendar implies.

        Malformed rows raise ValueError naming the bar; callers turn that into a 422.
        """
        today = today or datetime.now(timezone.utc).date()
        # Read, grade and rewrite under one lock: an entry appended between the read and the
        # rewrite used to be dropped from the file.
        with _lock_for(self.path):
            entries = self._read()
            for e in entries:
                series = prices_by_instrument.get(e.instrument)
                if not series:
                    continue
                call_day = _date(e.created_at[:10])
                bars = self._bars(series, e.instrument)
                ref_day, ref_close = self._reference(e, bars, call_day)
                factor = 1.0
                if ref_close is not None and abs(ref_close / e.price_at_call - 1) > ADJUSTMENT_TOLERANCE:
                    factor = ref_close / e.price_at_call
                window_bars = [(day, bar) for day, bar in bars if day > ref_day]
                if not window_bars:
                    continue
                last_day = window_bars[-1][0].toordinal()
                levels = (e.price_at_call * factor, e.invalidation * factor,
                          e.first_target * factor)
                for h in HORIZONS_DAYS:
                    key = str(h)
                    if key in e.outcomes and e.outcomes[key].get("price_source") != UNFETCHED:
                        continue
                    target = ref_day.toordinal() + h
                    if last_day <= target and today.toordinal() <= target:
                        continue            # the horizon has not closed yet
                    window = [(d, b) for d, b in window_bars if d.toordinal() <= target]
                    if not window:
                        continue
                    e.outcomes[key] = self._outcome(
                        e, window, h, adjusted, source, levels, factor, ref_day)
                e.settled = all(str(h) in e.outcomes for h in HORIZONS_DAYS)
            if persist:
                self._rewrite(entries)
        return entries

    @staticmethod
    def _outcome(e: Entry, window: list[tuple[date, dict]], h: int, adjusted: bool,
                 source: str, levels: tuple[float, float, float], factor: float,
                 ref_day: date) -> dict:
        entry, stop, target = levels
        stopped = False
        stop_index = len(window)
        exit_price = window[-1][1]["close"]
        exit_day = window[-1][1]["date"]
        basis = "not_stopped"
        for index, (_day, bar) in enumerate(window):
            if bar["low"] <= stop:
                stopped = True
                stop_index = index
                if bar["open"] <= stop:
                    exit_price, basis = bar["open"], "gap_open"
                else:
                    exit_price, basis = stop, "intrabar_stop"
                exit_day = bar["date"]
                break

        # The target only counts if it was reached while the position still existed: before the
        # stop bar, or at that bar's open. Bars after a stop-out used to be counted.
        before_stop = window[:stop_index]
        stop_bar = window[stop_index] if stopped else None
        hit_target = any(bar["high"] >= target for _day, bar in before_stop) or (
            stop_bar is not None and stop_bar[1]["open"] >= target
        )

        suspected = None
        prev = None
        for _day, bar in window:
            if prev:
                change = bar["close"] / prev - 1
                if change < -0.25 and bar["low"] > stop:
                    suspected = f"drop >25% on {bar['date']} without touching the stop"
                    break
                if change > 0.25:
                    suspected = f"jump >25% on {bar['date']}: check for an unadjusted split"
                    break
            prev = bar["close"]

        # "gap-adjusted" only when every bar in the window carried real open/high/low; a dict
        # of date and close alone must not be labelled as if stops had been simulated on it.
        has_ohlc = all(bar["ohlc"] for _day, bar in window)
        expected = max(1, int(h * 5 / 7) - 1)
        return {
            "horizon_days": h,
            "entry_date": ref_day.isoformat(),
            "price": round(window[-1][1]["close"], 4),
            "exit_price": round(exit_price, 4),
            "exit_date": str(exit_day)[:10],
            "return": round(exit_price / entry - 1, 4),
            "return_hold": round(window[-1][1]["close"] / entry - 1, 4),
            "hit_target": hit_target,
            "stopped": stopped,
            "stopped_basis": basis if has_ohlc else "close_only",
            "stop_policy": "gap_adjusted_stop" if has_ohlc else "close_only_no_gap_model",
            "bars": len(window),
            "sparse": len(window) < expected * 0.8,
            "adjusted_for_splits": adjusted,
            "levels_adjusted_by": round(factor, 6),
            "price_source": source,
            "corporate_action_suspected": suspected,
        }

    def _rewrite(self, entries: Iterable[Entry]) -> None:
        # A unique temp name: two graders sharing one "<tenant>.tmp" collided on Windows and
        # interleaved on Linux.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(e.to_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if self._damaged_lines:
            # Nothing is deleted silently: unreadable lines are moved aside, verbatim.
            with self.path.with_name(self.path.name + ".damaged").open("a", encoding="utf-8") as fh:
                fh.writelines(self._damaged_lines)
            self.damaged = 0
            self._damaged_lines = []


def summary(entries: Iterable[Entry]) -> dict:
    """Aggregate outcomes — including the losers, which is the point.

    Two kinds of entry are counted but not averaged. A card whose verdict was `pass` sized no
    position, so grading it as a taken long would be fiction. An entry flagged as a suspected
    corporate action is not a trading outcome. Both are reported under `excluded`.
    """
    rows = list(entries)
    out: dict[str, dict] = {}
    for h in HORIZONS_DAYS:
        graded = [e for e in rows if str(h) in e.outcomes]
        if not graded:
            continue
        # One pass, by field: comparing Entry objects by value would treat two identical
        # entries as the same one.
        flagged = sum(
            1 for e in graded if e.outcomes[str(h)].get("corporate_action_suspected")
        )
        taken = [e for e in graded
                 if not e.outcomes[str(h)].get("corporate_action_suspected")
                 and (e.verdict or "").lower() != "pass"]
        rets = [e.outcomes[str(h)]["return"] for e in taken]
        out[str(h)] = {
            "n": len(taken),
            "excluded": {
                "no_position": len(graded) - flagged - len(taken),
                "corporate_action_flags": flagged,
            },
            "gap_fills": sum(
                1 for e in taken if e.outcomes[str(h)].get("stopped_basis") == "gap_open"
            ),
            "avg_return": round(sum(rets) / len(rets), 4) if rets else None,
            "hit_target_rate": (round(sum(1 for e in taken if e.outcomes[str(h)]["hit_target"])
                                      / len(taken), 4) if taken else None),
            "stopped_rate": (round(sum(1 for e in taken if e.outcomes[str(h)]["stopped"])
                                   / len(taken), 4) if taken else None),
            "worst": round(min(rets), 4) if rets else None,
            "best": round(max(rets), 4) if rets else None,
        }
    return out
