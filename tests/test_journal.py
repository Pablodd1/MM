"""Journal tests: append-only storage, honest scoring, losers counted."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from mmdesk.journal import Journal, summary
from mmdesk.standard import Bars, standard_lens


def _card(ticker="AAA", price=100.0, inval=90.0, target=130.0, verdict="add"):
    return {
        "instrument": ticker,
        "created_at": "2026-01-02T15:00:00+00:00",
        "mode": "swing",
        "verdict": verdict,
        "risk": {"invalidation": inval, "first_target": target},
        "standard_lens": {"price": price},
    }


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


def test_scoring_counts_a_loss_honestly(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("LOSE", price=100, inval=95, target=120))
    start = date(2026, 1, 2)
    series = [((start + timedelta(days=i)).isoformat(), 100 - i * 0.5) for i in range(1, 40)]
    scored = j.score_entries({"LOSE": series})
    out = scored[0].outcomes["30"]
    assert out["return"] < 0
    assert out["stopped"] is True          # 95 was crossed
    assert out["hit_target"] is False


def test_scoring_marks_a_winner(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("WIN", price=100, inval=95, target=120))
    start = date(2026, 1, 2)
    series = [((start + timedelta(days=i)).isoformat(), 100 + i * 0.8) for i in range(1, 40)]
    scored = j.score_entries({"WIN": series})
    out = scored[0].outcomes["30"]
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
    start = date(2026, 1, 2)
    # dips to 96, then 95 (stop hit), then recovers to 118 by day 30
    prices = [96, 95, 100, 103] + [100 + i for i in range(1, 24)]
    series = [((start + timedelta(days=i)).isoformat(), float(p)) for i, p in enumerate(prices, 1)]
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
    start = date(2026, 1, 2)
    series = [
        ((start + timedelta(days=1)).isoformat(), 99, 100, 98, 99),
        ((start + timedelta(days=2)).isoformat(), 90, 91, 88, 90),   # opens below the stop
        ((start + timedelta(days=3)).isoformat(), 92, 93, 91, 92),
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
    start = date(2026, 1, 2)
    series = [
        ((start + timedelta(days=1)).isoformat(), 100, 101, 99, 100),
        ((start + timedelta(days=2)).isoformat(), 99, 100, 94, 98),  # wick through, opens above
    ]
    out = j.score_entries({"WICK": series})[0].outcomes["30"]
    assert out["stopped"] is True
    assert out["stopped_basis"] == "intrabar_stop"
    assert out["exit_price"] == pytest.approx(95.0)


def test_unadjusted_split_is_flagged_not_scored_as_a_loss(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("SPLIT", price=100, inval=45, target=200))
    start = date(2026, 1, 2)
    series = [
        ((start + timedelta(days=1)).isoformat(), 100, 101, 99, 100),
        ((start + timedelta(days=2)).isoformat(), 50, 51, 49, 50),   # 2-for-1, unadjusted
    ]
    out = j.score_entries({"SPLIT": series})[0].outcomes["30"]
    assert out["stopped"] is False
    assert out["corporate_action_suspected"] is not None
    assert out["adjusted_for_splits"] is False


def test_summary_reports_worst_case(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("A", inval=50, target=200))
    j.append(_card("B", inval=50, target=200))
    start = date(2026, 1, 2)
    up = [((start + timedelta(days=i)).isoformat(), 100 + i) for i in range(1, 40)]
    down = [((start + timedelta(days=i)).isoformat(), 100 - i) for i in range(1, 40)]
    scored = j.score_entries({"A": up, "B": down})
    s = summary(scored)["30"]
    assert s["n"] == 2
    assert s["worst"] < 0 < s["best"]


def test_scoring_is_idempotent(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.append(_card("X"))
    start = date(2026, 1, 2)
    series = [((start + timedelta(days=i)).isoformat(), 101.0) for i in range(1, 40)]
    first = j.score_entries({"X": series})[0].outcomes["30"]
    second = j.score_entries({"X": series})[0].outcomes["30"]
    assert first == second


def test_grade_endpoint_uses_fetched_ohlc(tmp_path, monkeypatch):
    """The endpoint must grade from fetched bars, not from caller-supplied prices."""
    from fastapi.testclient import TestClient
    from mmdesk import service
    from mmdesk.standard import Bars

    journal_path = tmp_path / "journal.jsonl"
    j = Journal(journal_path)
    j.append({
        "instrument": "TEST", "created_at": "2026-01-02T21:00:00+00:00",
        "verdict": "add", "mode": "swing",
        "standard_lens": {"price": 100.0},
        "risk": {"invalidation": 95.0, "first_target": 130.0},
    })

    dates = [f"2026-02-{d:02d}" for d in range(1, 6)]
    bars = Bars(close=[99, 97, 94, 98, 101],
                high=[100, 98, 95, 99, 102],
                low=[98, 96, 93, 97, 100],
                open=[99, 97, 94, 98, 101],
                dates=dates, source="stub")

    class StubGrounding:
        bars = None
        missing: list = []

    class StubGrounder:
        def ground(self, ticker, fundamentals=True):
            g = StubGrounding()
            g.bars = bars
            return g

    monkeypatch.setattr(service, "JOURNAL_PATH", journal_path)
    monkeypatch.setattr(service, "Grounder", StubGrounder)

    r = TestClient(service.app).post("/api/journal/grade")
    assert r.status_code == 200
    body = r.json()
    assert body["graded"] == 1
    outcome = body["entries"][0]["outcomes"]
    assert outcome, "an entry with bars after the call must be graded"
    first = next(iter(outcome.values()))
    assert first["stopped_basis"] in ("not_stopped", "close_only", "intrabar_stop", "gap_open")
    assert first["stop_policy"] == "gap_adjusted_stop"   # OHLC came through, not close-only
    assert first["price_source"] == "yfinance auto_adjust"
