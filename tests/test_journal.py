"""Journal tests: append-only storage, honest scoring, losers counted.

The journal is the app's honesty mechanism, so the tests here are mostly about ways it could
lie: a horizon frozen before it closed, a window that skips the first session, a split graded as
a stop-out, a target counted after the position was gone, and outcomes written by whoever calls
the endpoint.

Dates in the scoring tests are explicit, and every expected number is worked out from the same
rule: the window is the bars after the price bar up to and including the horizon date.
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest
from conftest import StubGrounder
from fastapi.testclient import TestClient

from mmdesk import service
from mmdesk.journal import Journal, summary
from mmdesk.standard import Bars

DAY = timedelta(days=1)
JAN2 = date(2026, 1, 2)
JAN9 = date(2026, 1, 9)
JAN12 = date(2026, 1, 12)
LATE = date(2026, 3, 30)          # "today" for tests that need every short horizon closed
CALLED = "2026-01-10T15:00:00+00:00"


def _card(ticker="AAA", price=100.0, inval=90.0, target=130.0, verdict="add",
          created="2026-01-02T15:00:00+00:00", price_bar=None):
    card = {
        "instrument": ticker,
        "created_at": created,
        "mode": "swing",
        "verdict": verdict,
        "risk": {"invalidation": inval, "first_target": target},
        "standard_lens": {"price": price},
    }
    if price_bar:
        card["grounding"] = {"price_bar": price_bar}
    return card


def _bars(start: date, closes, opens=None, highs=None, lows=None):
    """OHLC rows; an unspecified open/high/low is the close."""
    closes = [float(c) for c in closes]
    opens = [float(o) for o in (opens or closes)]
    highs = [float(h) for h in (highs or [max(o, c) for o, c in zip(opens, closes, strict=True)])]
    lows = [float(low) for low in (lows or [min(o, c) for o, c in zip(opens, closes, strict=True)])]
    return [[(start + i * DAY).isoformat(), o, h, low, c]
            for i, (o, h, low, c) in enumerate(zip(opens, highs, lows, closes, strict=True))]


# ---------------------------------------------------------------- storage

def test_append_is_append_only(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    e1 = j.append(_card("AAA"))
    e2 = j.append(_card("BBB"))
    assert e1.id != e2.id
    lines = (tmp_path / "j.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [e.instrument for e in j.all()] == ["AAA", "BBB"]


def test_card_without_price_is_rejected(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    with pytest.raises(ValueError):
        j.append({"instrument": "AAA", "risk": {"invalidation": 1, "first_target": 2}})
    with pytest.raises(ValueError):
        j.append({"instrument": "AAA", "standard_lens": {"price": 10}})


def test_price_bar_is_recorded_with_the_call(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card(price_bar="2026-01-02"))
    assert j.all()[0].price_bar_date == "2026-01-02"


# ---------------------------------------------------------------- when an outcome may be written

def test_unelapsed_horizon_is_not_recorded(tmp_path):
    """Graded the day after the call, every horizon used to be frozen at a one-day move —
    and marked settled."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("FRZ", created=CALLED, price_bar="2026-01-09"))
    scored = j.score_entries({"FRZ": [["2026-01-09", 100.0], ["2026-01-12", 101.0]]},
                             today=date(2026, 1, 13))
    assert scored[0].outcomes == {}
    assert scored[0].settled is False
    assert summary(scored) == {}


def test_outcome_is_written_once_the_series_passes_the_horizon(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("FRZ", created=CALLED, price_bar="2026-01-09"))
    series = {"FRZ": [["2026-01-09", 100.0]] + _bars(JAN12, [100 + 2 * i for i in range(45)])}
    scored = j.score_entries(series, today=date(2026, 1, 13))
    out = scored[0].outcomes
    assert "30" in out, "the data already runs past day 30, so it can be graded"
    assert "365" not in out
    # day 30 after Jan 9 is Feb 8: bars Jan 12 .. Feb 8 inclusive, i = 0 .. 27
    assert out["30"]["bars"] == 28
    assert out["30"]["hit_target"] is True       # 130 is reached on i = 15 (Jan 27)


def test_hit_target_refreshes_when_the_window_completes(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("FRZ", created=CALLED, price_bar="2026-01-09"))
    early = j.score_entries({"FRZ": [["2026-01-09", 100.0], ["2026-01-12", 99.0]]},
                            today=date(2026, 1, 13))
    assert early[0].outcomes == {}
    series = {"FRZ": [["2026-01-09", 100.0]] + _bars(JAN12, [99 + 2 * i for i in range(45)])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["hit_target"] is True
    assert out["price"] == 153.0                 # i = 27 -> 99 + 54


def test_horizon_is_measured_from_the_price_bar(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("FRZ", created=CALLED, price_bar="2026-01-02"))
    out = j.score_entries({"FRZ": _bars(JAN2, [100 + i for i in range(45)])}, today=LATE)[0]
    out = out.outcomes["30"]
    # day 30 after Jan 2 is Feb 1: bars Jan 3 .. Feb 1 inclusive = 30 bars
    assert out["bars"] == 30
    assert out["entry_date"] == "2026-01-02"


# ---------------------------------------------------------------- the window and the levels

def test_window_starts_after_the_price_bar_not_the_call_date(tmp_path):
    """A pre-market call on Jan 6 priced off the Jan 5 close lost the Jan 6 session, because the
    window began after the call's calendar date. That hid same-day stop-outs."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("EARLY", inval=95.0, created="2026-01-06T10:00:00+00:00",
                   price_bar="2026-01-05"))
    series = {"EARLY": [["2026-01-05", 100.0, 100, 100, 100]] + _bars(
        date(2026, 1, 6), [98, 99, 99.5, 100],
        opens=[94, 98.5, 99, 99.5], lows=[93, 98, 98.5, 99])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["stopped_basis"] == "gap_open"     # Jan 6 opens at 94, through the 95 stop
    assert out["return"] == pytest.approx(-0.06, abs=1e-6)
    assert out["bars"] == 4


def test_an_older_call_is_anchored_on_the_bar_matching_its_price(tmp_path):
    """Entries written before the price bar was recorded: the latest bar at or before the call
    whose close is the recorded price."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("LEGACY", inval=95.0, created="2026-01-06T10:00:00+00:00"))   # evening call
    series = {"LEGACY": _bars(JAN2, [100.0, 100.0, 100.0, 100.0, 100.0, 94.0, 95.0, 96.0])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["stopped"] is True and out["stopped_basis"] == "gap_open"


def test_split_after_the_call_rescales_levels_not_a_fabricated_stop(tmp_path):
    """The provider's bars are adjusted; the stored stop is not. A 2-for-1 split used to read as
    a fifty percent stop-out."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("SPLIT", inval=95.0, target=130.0, created=CALLED, price_bar="2026-01-09"))
    series = {"SPLIT": [["2026-01-09", 50.0]] + _bars(JAN12, [50 + 0.1 * i for i in range(45)])}
    out = j.score_entries(series, adjusted=True, today=LATE)[0].outcomes["30"]
    assert out["stopped"] is False
    assert out["levels_adjusted_by"] == pytest.approx(0.5)
    assert out["return"] == pytest.approx(0.054, abs=1e-3)     # 52.7 against a 50 entry


def test_an_unadjusted_reverse_split_is_flagged(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("REV", inval=95.0, target=130.0, created=CALLED, price_bar="2026-01-09"))
    series = {"REV": [["2026-01-09", 100.0]] + _bars(JAN12, [100, 240, 241, 242])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["corporate_action_suspected"] is not None
    assert "jump >25%" in out["corporate_action_suspected"]


# ---------------------------------------------------------------- stop and target

def test_scoring_counts_a_loss_honestly(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("LOSE", price=100, inval=95, target=120))
    series = [((JAN2 + i * DAY).isoformat(), 100 - i * 0.5) for i in range(1, 40)]
    out = j.score_entries({"LOSE": series})[0].outcomes["30"]
    assert out["return"] < 0
    assert out["stopped"] is True          # 95 was crossed
    assert out["hit_target"] is False


def test_scoring_marks_a_winner(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("WIN", price=100, inval=95, target=120))
    series = [((JAN2 + i * DAY).isoformat(), 100 + i * 0.8) for i in range(1, 40)]
    out = j.score_entries({"WIN": series})[0].outcomes["30"]
    assert out["return"] > 0
    assert out["hit_target"] is True


def test_unknown_instrument_is_left_unscored(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("NODATA"))
    scored = j.score_entries({})
    assert scored[0].outcomes == {}
    assert scored[0].settled is False


def test_stopped_is_recorded_even_if_it_recovers(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("WHIP", price=100, inval=95, target=130))
    # dips to 96, then 95 (stop hit), then recovers to 118 by day 30
    prices = [96, 95, 100, 103] + [100 + i for i in range(1, 24)]
    series = [((JAN2 + i * DAY).isoformat(), float(p)) for i, p in enumerate(prices, 1)]
    out = j.score_entries({"WHIP": series})[0].outcomes["30"]
    assert out["stopped"] is True          # the trade would have been stopped out
    assert out["stopped_basis"] == "close_only"
    # `return` is what the trade actually produced: stopped at 95 from 100, a 5% loss.
    # `return_hold` is the price only if you had ignored the stop, which is the number the
    # journal must NOT report as the outcome.
    assert out["return"] == pytest.approx(-0.05, abs=1e-6)
    assert out["return_hold"] > 0


def test_gap_through_the_stop_fills_at_the_open(tmp_path):
    """A stop order does not fill at the stop when the market opens below it."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("GAP", price=100, inval=95, target=130))
    series = [
        ((JAN2 + 1 * DAY).isoformat(), 99, 100, 98, 99),
        ((JAN2 + 2 * DAY).isoformat(), 90, 91, 88, 90),   # opens below the stop
        ((JAN2 + 3 * DAY).isoformat(), 92, 93, 91, 92),
    ]
    out = j.score_entries({"GAP": series})[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["stopped_basis"] == "gap_open"
    assert out["exit_price"] == pytest.approx(90.0)     # not 95
    assert out["return"] == pytest.approx(-0.10, abs=1e-6)
    assert out["stop_policy"] == "gap_adjusted_stop"


def test_intrabar_stop_fills_at_the_stop(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("WICK", price=100, inval=95, target=130))
    series = [
        ((JAN2 + 1 * DAY).isoformat(), 100, 101, 99, 100),
        ((JAN2 + 2 * DAY).isoformat(), 99, 100, 94, 98),  # wick through, opens above
    ]
    out = j.score_entries({"WICK": series})[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["stopped_basis"] == "intrabar_stop"
    assert out["exit_price"] == pytest.approx(95.0)


def test_hit_target_ignores_bars_after_the_stop(tmp_path):
    """The position is gone once the stop fills; a later rally is not a win on this trade."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("AFTER", price=100, inval=95, target=120, created=CALLED, price_bar="2026-01-09"))
    closes = [98] + [119 + 0.5 * i for i in range(30)]
    series = {"AFTER": [["2026-01-09", 100.0]] + _bars(
        JAN12, closes, opens=[99] + closes[1:], lows=[94] + closes[1:])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["hit_target"] is False


def test_one_bar_through_stop_and_target_does_not_claim_both(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("BOTH", price=100, inval=94, target=120, created=CALLED, price_bar="2026-01-09"))
    series = {"BOTH": [["2026-01-09", 100.0]] + _bars(
        JAN12, [110], opens=[100], highs=[135], lows=[93])}
    out = j.score_entries(series, today=LATE)[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["hit_target"] is False


def test_unadjusted_split_is_flagged_not_scored_as_a_loss(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("SPLIT", price=100, inval=45, target=200))
    series = [
        ((JAN2 + 1 * DAY).isoformat(), 100, 101, 99, 100),
        ((JAN2 + 2 * DAY).isoformat(), 50, 51, 49, 50),   # 2-for-1, unadjusted
    ]
    out = j.score_entries({"SPLIT": series})[0].outcomes["30"]
    assert out["stopped"] is False
    assert out["corporate_action_suspected"] is not None
    assert out["adjusted_for_splits"] is False


def test_close_only_dict_rows_are_labelled_close_only(tmp_path):
    """A dict of date and close carries no open/high/low; it must not be graded as if stops had
    been simulated on it."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("DICT", created=CALLED, price_bar="2026-01-09"))
    rows = [{"date": "2026-01-09", "close": 100.0}] + [
        {"date": (JAN12 + i * DAY).isoformat(), "close": 100.0 + i} for i in range(40)]
    out = j.score_entries({"DICT": rows}, today=LATE)[0].outcomes["30"]
    assert out["stopped_basis"] == "close_only"
    assert out["stop_policy"] == "close_only_no_gap_model"


def test_rows_are_sorted_and_deduplicated_by_date(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("SORT", created=CALLED, price_bar="2026-01-09"))
    rows = _bars(JAN12, [100 + i for i in range(40)])
    shuffled = [["2026-01-09", 100.0]] + rows[20:] + rows[:20] + [rows[0]]   # unordered, one duplicate
    out = j.score_entries({"SORT": shuffled}, today=LATE)[0].outcomes["30"]
    assert out["bars"] == 28
    assert out["price"] == 127.0                      # i = 27 -> 100 + 27


def test_summary_excludes_no_position_and_flagged_entries(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("TAKE", created=CALLED, price_bar="2026-01-09"))
    j.append(_card("SKIP", verdict="pass", created=CALLED, price_bar="2026-01-09"))
    j.append(_card("FLAG", inval=45, created=CALLED, price_bar="2026-01-09"))
    rising = [["2026-01-09", 100.0]] + _bars(JAN12, [100 + i for i in range(40)])
    halved = [["2026-01-09", 100.0], ["2026-01-12", 100.0]] + _bars(date(2026, 1, 13), [50] * 39)
    scored = j.score_entries({"TAKE": rising, "SKIP": rising, "FLAG": halved}, today=LATE)
    s = summary(scored)["30"]
    assert s["n"] == 1
    assert s["excluded"] == {"no_position": 1, "corporate_action_flags": 1}
    assert s["avg_return"] == pytest.approx(0.27)


def test_scoring_is_idempotent(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("X"))
    series = [((JAN2 + i * DAY).isoformat(), 101.0) for i in range(1, 40)]
    first = j.score_entries({"X": series})[0].outcomes["30"]
    second = j.score_entries({"X": series})[0].outcomes["30"]
    assert first == second


def test_grade_replaces_an_outcome_no_provider_produced(tmp_path):
    """Before scoring became read-only, /api/journal/score wrote outcomes that the real grader
    then refused to touch. Those are re-graded."""
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("OLD", created=CALLED, price_bar="2026-01-09"))
    forged = [["2026-01-09", 100.0]] + _bars(JAN12, [9999.0] * 40)
    j.score_entries({"OLD": forged})
    assert j.all()[0].outcomes["30"]["return"] > 98
    series = {"OLD": [["2026-01-09", 100.0]] + _bars(JAN12, [101.0] * 40)}
    j.score_entries(series, adjusted=True, source="provider")
    out = j.all()[0].outcomes["30"]
    assert out["price_source"] == "provider"
    assert out["return"] == pytest.approx(0.01)


def test_summary_reports_worst_case(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("A", inval=50, target=200))
    j.append(_card("B", inval=50, target=200))
    up = [((JAN2 + i * DAY).isoformat(), 100 + i) for i in range(1, 40)]
    down = [((JAN2 + i * DAY).isoformat(), 100 - i) for i in range(1, 40)]
    s = summary(j.score_entries({"A": up, "B": down}))["30"]
    assert s["n"] == 2
    assert s["worst"] < 0 < s["best"]


# ---------------------------------------------------------------- the record cannot be broken

def test_damaged_line_does_not_break_the_journal(tmp_path):
    """One truncated line used to raise on every read — and, because append reads too, it stopped
    the tenant from analysing anything."""
    path = tmp_path / "j.jsonl"
    j = Journal(path)
    j.append(_card("OK", created=CALLED, price_bar="2026-01-09"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "trunc", "instrument": "AAA", "outcom\n')   # a torn write
        fh.write('{"id": "inf", "created_at": "2026-01-02", "instrument": "Z", "mode": "swing",'
                 ' "verdict": "add", "price_at_call": 100.0, "invalidation": 90.0,'
                 ' "first_target": 130.0, "outcomes": {"30": {"return": Infinity}}}\n')
    assert [e.instrument for e in j.all()] == ["OK"]
    assert j.damaged == 2
    j.append(_card("OK2", created=CALLED, price_bar="2026-01-09"))
    assert len(j.all()) == 2

    # A rewrite moves the unreadable lines aside verbatim; nothing is deleted silently.
    series = {"OK": [["2026-01-09", 100.0]] + _bars(JAN12, [100.0] * 40)}
    j.score_entries(series, today=LATE)
    assert j.damaged == 0
    assert "trunc" in (tmp_path / "j.jsonl.damaged").read_text(encoding="utf-8")


def test_append_during_scoring_is_not_lost(tmp_path):
    """`score_entries` reads, grades and rewrites. An entry appended in between was erased."""
    path = tmp_path / "j.jsonl"
    j = Journal(path)
    for name in ("A1", "A2"):
        j.append(_card(name, created=CALLED, price_bar="2026-01-09"))
    series = {name: [["2026-01-09", 100.0]] + _bars(JAN12, [100.0] * 40) for name in ("A1", "A2")}
    grading = threading.Event()
    original = Journal._outcome

    def slow_outcome(*args, **kwargs):
        grading.set()
        time.sleep(0.2)          # long enough for a concurrent append to try to land
        return original(*args, **kwargs)

    Journal._outcome = staticmethod(slow_outcome)
    try:
        grader = threading.Thread(target=lambda: j.score_entries(series, today=LATE))
        grader.start()
        assert grading.wait(5)
        j.append(_card("A3", created=CALLED, price_bar="2026-01-09"))
        grader.join(10)
        assert not grader.is_alive()
    finally:
        Journal._outcome = staticmethod(original)
    assert [e.instrument for e in j.all()] == ["A1", "A2", "A3"]
    assert j.all()[0].outcomes and j.all()[2].outcomes == {}


def test_non_finite_and_non_positive_prices_are_refused(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("AAA"))
    bad_series = ([["2026-01-03", float("inf")]], [["2026-01-03", -1.0]], [["2026-01-03", 0]],
                  [["2026-01-03"]], [["2026-01-03", 100, 101]], [["x", 100.0]], ["2026-01-03"])
    for bad in bad_series:
        with pytest.raises(ValueError):
            j.score_entries({"AAA": bad})
    assert len(j.all()) == 1 and j.all()[0].outcomes == {}


# ---------------------------------------------------------------- the endpoints

def test_grade_endpoint_uses_fetched_ohlc(monkeypatch, tmp_path):
    """The endpoint must grade from fetched bars, not from caller-supplied prices."""
    journal_path = tmp_path / "journal.jsonl"
    Journal(journal_path).append({
        "instrument": "TEST", "created_at": "2026-01-02T21:00:00+00:00",
        "verdict": "add", "mode": "swing",
        "standard_lens": {"price": 100.0},
        "risk": {"invalidation": 95.0, "first_target": 130.0},
    })
    dates = [f"2026-02-{d:02d}" for d in range(1, 6)]
    fetched = Bars(close=[99, 97, 94, 98, 101], high=[100, 98, 95, 99, 102],
                   low=[98, 96, 93, 97, 100], open=[99, 97, 94, 98, 101],
                   dates=dates, source="stub")

    class Fetched:
        bars = fetched
        missing: list = []

    class Fetcher:
        def ground(self, ticker, fundamentals=True):
            return Fetched()

    monkeypatch.setattr(service, "_journal", lambda tenant, scope=None: Journal(journal_path))
    monkeypatch.setattr(service, "Grounder", Fetcher)

    body = TestClient(service.app).post("/api/journal/grade").json()
    assert body["graded"] == 1
    outcome = body["entries"][0]["outcomes"]
    assert outcome, "an entry with bars after the call must be graded"
    first = next(iter(outcome.values()))
    # day 30 after a Jan 2 call is Feb 1: the one bar in the window never nears the stop
    assert first["bars"] == 1
    assert first["stopped_basis"] == "not_stopped"
    assert first["stop_policy"] == "gap_adjusted_stop"   # OHLC came through, not close-only
    assert first["price_source"] == "yfinance auto_adjust"


def _journal_client(tmp_path, monkeypatch, **client_kwargs):
    """A journal under the test's own directory, with offline bars for any analyse call."""
    journal_dir = tmp_path / "journal"

    def open_journal(tenant, scope=None):
        if scope:
            return Journal(journal_dir / tenant.id / f"{scope}.jsonl")
        return Journal(journal_dir / f"{tenant.id}.jsonl")

    monkeypatch.setattr(service, "_journal", open_journal)
    monkeypatch.setattr(service, "Grounder", StubGrounder)
    return TestClient(service.app, **client_kwargs), journal_dir / "default.jsonl"


def test_score_endpoint_returns_422_for_malformed_rows(tmp_path, monkeypatch):
    client, path = _journal_client(tmp_path, monkeypatch)
    Journal(path).append(_card("AAA", created=CALLED, price_bar="2026-01-09"))
    bad_rows = [
        [["2026-01-03"]],                      # date only
        [["2026-01-03", "x"]],                 # not a number
        [["03-01-2026", 100.0]],               # not ISO
        [["2026-01-03", -1.0]],                # negative
        [["2026-01-03", 0]],                   # zero
        [{"date": "2026-01-03"}],              # no close
        [["2026-01-03", 1, 2, 3]],             # wrong arity
        ["2026-01-03"],                        # a string, not a row
    ]
    for rows in bad_rows:
        r = client.post("/api/journal/score", json={"prices": {"AAA": rows}})
        assert r.status_code == 422, rows
        assert r.json()["detail"]["error"] == "bad_bars"
    # `1e999` is valid JSON that parses to infinity; the test client will not serialise inf itself.
    overflow = client.post("/api/journal/score",
                           content='{"prices": {"AAA": [["2026-01-03", 1e999]]}}',
                           headers={"Content-Type": "application/json"})
    assert overflow.status_code == 422
    for body in ({"prices": 5}, {"prices": []}, {}, {"prices": {"AAA": "x"}}):
        assert client.post("/api/journal/score", json=body).status_code == 422


def test_score_endpoint_never_writes(tmp_path, monkeypatch):
    """One request with an invented price used to write a permanent outcome into the record."""
    client, path = _journal_client(tmp_path, monkeypatch)
    Journal(path).append(_card("AAA", created=CALLED, price_bar="2026-01-09"))
    before = path.read_text(encoding="utf-8")
    forged = [["2026-01-09", 100.0]] + [[(JAN12 + i * DAY).isoformat(), 9999.0] for i in range(40)]
    r = client.post("/api/journal/score", json={"prices": {"AAA": forged}})
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] is False
    assert body["summary"]["30"]["avg_return"] > 98          # the what-if is still shown
    assert path.read_text(encoding="utf-8") == before        # and the record is untouched
    assert client.get("/api/journal").json()["entries"][0]["outcomes"] == {}


def test_journal_is_private_to_its_scope(tmp_path, monkeypatch):
    """A journal names positions and, through the sizing, the account behind it. A stranger
    used to be able to read it with nothing but the tenant id."""
    client, shared = _journal_client(tmp_path, monkeypatch)
    Journal(shared).append(_card("AAA", created=CALLED, price_bar="2026-01-09"))

    stranger = TestClient(service.app, client=("203.0.113.9", 50000))
    for response in (stranger.get("/api/journal"), stranger.post("/api/journal/grade"),
                     stranger.post("/api/journal/score",
                                   json={"prices": {"AAA": [["2026-01-03", 1.0]]}})):
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "journal_private"

    # The same stranger, working under its own scope, sees only its own calls.
    own_headers = {"X-Journal-Id": "browser-7f3a9c2e"}
    assert stranger.post("/api/analyze", headers=own_headers,
                         json={"ticker": "MSFT"}).status_code == 200
    own = stranger.get("/api/journal", headers=own_headers).json()
    assert own["scope"] == "own" and own["count"] == 1
    assert own["entries"][0]["instrument"] == "MSFT"

    # Loopback (the operator) still sees the shared file, which the stranger's call did not touch.
    shared_view = client.get("/api/journal").json()
    assert shared_view["scope"] == "shared"
    assert [e["instrument"] for e in shared_view["entries"]] == ["AAA"]

    # So does the admin token — and only the admin token.
    monkeypatch.setenv("MMDESK_ADMIN_TOKEN", "operator-token")
    admin = stranger.get("/api/journal", headers={"X-Admin-Token": "operator-token"}).json()
    assert [e["instrument"] for e in admin["entries"]] == ["AAA"]
    wrong = stranger.get("/api/journal", headers={"X-Admin-Token": "wrong"})
    assert wrong.status_code == 403


def test_journal_scopes_do_not_see_each_other(tmp_path, monkeypatch):
    client, shared = _journal_client(tmp_path, monkeypatch)
    alice = {"X-Journal-Id": "alice-scope-0001"}
    bob = {"X-Journal-Id": "bob-scope-000002"}
    assert client.post("/api/analyze", headers=alice, json={"ticker": "MSFT"}).status_code == 200
    for _ in range(2):
        assert client.post("/api/analyze", headers=bob,
                           json={"ticker": "NVDA"}).status_code == 200
    assert client.get("/api/journal", headers=alice).json()["count"] == 1
    assert client.get("/api/journal", headers=bob).json()["count"] == 2
    assert not shared.exists(), "scoped calls must not land in the shared journal"


def test_bad_journal_scope_is_refused(tmp_path, monkeypatch):
    client, _ = _journal_client(tmp_path, monkeypatch)
    for bad in ("../etc", "short", "x" * 65, "has space"):
        r = client.get("/api/journal", headers={"X-Journal-Id": bad})
        assert r.status_code == 422, bad
        assert r.json()["detail"]["error"] == "bad_journal_id"


def test_serverless_journal_is_not_readable_from_loopback_alone(monkeypatch):
    """`VERCEL` set: the peer address is whatever the platform reports, so loopback alone
    proves nothing and is no longer enough."""
    monkeypatch.setenv("VERCEL", "1")
    client = TestClient(service.app)          # reports itself as "testclient", a loopback name
    assert client.get("/api/journal").status_code == 403
    scoped = client.get("/api/journal", headers={"X-Journal-Id": "browser-7f3a9c2e"})
    assert scoped.status_code == 200
    assert client.post("/api/tenants", json={"id": "acme", "name": "Acme"}).status_code == 403
