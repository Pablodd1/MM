"""Rewrite journal.score_entries with gap-aware stops and corporate-action flags."""

from pathlib import Path

p = Path("mmdesk/journal.py")
s = p.read_text(encoding="utf-8")

NEW = '''    @staticmethod
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
'''

start = s.index("    def score_entries(")
end = s.index("        # rewrite whole file atomically")
s = s[:start] + NEW + "\n" + s[end:]
p.write_text(s, encoding="utf-8")
print("journal rewritten:", "_bar" in s and "gap_open" in s)
