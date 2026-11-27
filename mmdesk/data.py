"""Market data grounding.

Every number the app acts on comes from here, with a provider name and the time
it was fetched. If a provider fails, the field goes into `missing` and the card says
so — the app never fills a gap with an estimate, a memory or a round number.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .standard import Bars

log = logging.getLogger("mmdesk.data")

CACHE_TTL_S = 300     # five minutes: enough for repeated questions, short enough to stay honest
CACHE_MAX_ENTRIES = 64
REQUEST_TIMEOUT_S = 10

# One cache per process for the default provider. A new Grounder used to be built for every
# request, each with its own empty cache, so the TTL never hit and the same bars were downloaded
# again for the analysis, the crowd and the second opinion of one question.
_SHARED_CACHE: dict[str, tuple[float, Grounding]] = {}
_SHARED_LOCK = threading.Lock()


@dataclass
class Grounding:
    ticker: str
    as_of: str
    bars: Bars | None = None
    fundamentals: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_bar: str | None = None     # date of the bar whose close is the current price

    @property
    def complete(self) -> bool:
        return not self.missing and self.bars is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "sources": self.sources,
            "missing": self.missing,
            "errors": self.errors,
            "complete": self.complete,
            "fundamentals": self.fundamentals,
            "price": self.bars.close[-1] if self.bars else None,
            "bars": len(self.bars.close) if self.bars else 0,
        }


class Provider(Protocol):
    name: str

    def daily_bars(self, ticker: str) -> tuple[Bars, dict]: ...
    def fundamentals(self, ticker: str) -> tuple[dict, dict]: ...


def _clean(value: object) -> float | None:
    """A provider's NaN or None is a missing bar, not a number to average in."""
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


class YFinanceProvider:
    """Free, no-key provider. Good for swing analysis; cannot support intraday."""

    name = "yfinance"

    def daily_bars(self, ticker: str) -> tuple[Bars, dict]:
        import yfinance as yf

        t = yf.Ticker(ticker)
        hist = t.history(period="2y", interval="1d", auto_adjust=True,
                         timeout=REQUEST_TIMEOUT_S)
        if hist is None or hist.empty:
            raise RuntimeError(f"no daily bars for {ticker}")
        rows = []
        for index, row in zip(hist.index, hist.itertuples(index=False), strict=True):
            open_, high, low, close = (_clean(v) for v in (row.Open, row.High, row.Low, row.Close))
            if close is None:
                continue
            rows.append((str(index.date()), open_ or close, high or close, low or close, close))
        if len(rows) < 2:
            raise RuntimeError(f"only {len(rows)} usable daily bars for {ticker}")
        dates, opens, highs, lows, closes = (list(column) for column in zip(*rows, strict=True))
        return (
            Bars(
                close=closes,
                high=highs,
                low=lows,
                source=self.name,
                open=opens,
                dates=dates,
            ),
            {"field": "daily_bars", "provider": self.name, "rows": len(closes),
             "last_bar": dates[-1]},
        )

    def fundamentals(self, ticker: str) -> tuple[dict, dict]:
        import yfinance as yf

        t = yf.Ticker(ticker)
        info = t.info or {}
        keep = {
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "price_to_book": info.get("priceToBook"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            # The provider's timestamp is the most recent report, not the next one, so between
            # reports this reads as "just reported". Known limitation, not a forecast.
            "earnings_date": info.get("earningsTimestamp"),
            "dividend_yield": info.get("dividendYield"),
        }
        return keep, {"field": "fundamentals", "provider": self.name,
                      "keys": [k for k, v in keep.items() if v is not None]}


class Grounder:
    def __init__(self, provider: Provider | None = None, ttl: float | None = None) -> None:
        self.provider = provider or YFinanceProvider()
        self.ttl = CACHE_TTL_S if ttl is None else ttl
        # A caller that injects a provider gets a private cache, so two tests with two stub
        # providers for the same ticker cannot read each other's grounding.
        self._cache: dict[str, tuple[float, Grounding]] = (
            {} if provider is not None else _SHARED_CACHE
        )
        self._lock = threading.Lock() if provider is not None else _SHARED_LOCK

    def ground(self, ticker: str, fundamentals: bool = True) -> Grounding:
        key = f"{ticker.upper()}:{fundamentals}"
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.ttl:
                return cached[1]

        g = Grounding(ticker=ticker.upper(),
                      as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        try:
            bars, src = self.provider.daily_bars(ticker)
            g.bars = bars
            g.last_bar = src.get("last_bar")
            g.sources.append(src)
        except Exception as exc:  # noqa: BLE001 - failure is data, not a crash
            g.missing.append("price")
            g.errors.append(f"{self.provider.name}: {exc}")
            log.warning("daily bars for %s failed: %s", ticker, exc)

        if fundamentals:
            try:
                f, src = self.provider.fundamentals(ticker)
                g.fundamentals = f
                g.sources.append(src)
                if f.get("trailing_pe") is None:
                    g.missing.append("valuation_multiple")
            except Exception as exc:  # noqa: BLE001
                g.missing.append("fundamentals")
                g.errors.append(f"{self.provider.name}: {exc}")
                log.warning("fundamentals for %s failed: %s", ticker, exc)

        with self._lock:
            if len(self._cache) >= CACHE_MAX_ENTRIES:
                oldest = min(self._cache, key=lambda k: self._cache[k][0], default=None)
                if oldest is not None:
                    del self._cache[oldest]
            self._cache[key] = (now, g)
        return g


def intraday_supported() -> bool:
    """Free daily data cannot power an intraday decision. The service states this."""
    return False
