"""Market data grounding.

Every number the app acts on comes from here, with a provider name and the time
it was fetched. If a provider fails, the field goes into `missing` and the card is
marked INCOMPLETE — the app never fills a gap with an estimate, a memory or a
round number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from .standard import Bars

CACHE_TTL_S = 300  # five minutes: enough for repeated questions, short enough to stay honest


@dataclass
class Grounding:
    ticker: str
    as_of: str
    bars: Bars | None = None
    fundamentals: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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


class YFinanceProvider:
    """Free, no-key provider. Good for swing analysis; cannot support intraday."""

    name = "yfinance"

    def daily_bars(self, ticker: str) -> tuple[Bars, dict]:
        import yfinance as yf

        t = yf.Ticker(ticker)
        hist = t.history(period="2y", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            raise RuntimeError(f"no daily bars for {ticker}")
        closes = [float(x) for x in hist["Close"].tolist()]
        return (
            Bars(
                close=closes,
                high=[float(x) for x in hist["High"].tolist()],
                low=[float(x) for x in hist["Low"].tolist()],
                volume=[float(x) for x in hist["Volume"].tolist()],
                source=self.name,
                open=[float(x) for x in hist["Open"].tolist()],
                dates=[str(d.date()) for d in hist.index],
            ),
            {"field": "daily_bars", "provider": self.name, "rows": len(closes),
             "last_bar": str(hist.index[-1].date())},
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
            "earnings_date": info.get("earningsTimestamp"),
            "dividend_yield": info.get("dividendYield"),
        }
        return keep, {"field": "fundamentals", "provider": self.name,
                      "keys": [k for k, v in keep.items() if v is not None]}


class Grounder:
    def __init__(self, provider: Provider | None = None) -> None:
        self.provider = provider or YFinanceProvider()
        self._cache: dict[str, tuple[float, Grounding]] = {}

    def ground(self, ticker: str, fundamentals: bool = True) -> Grounding:
        key = f"{ticker.upper()}:{fundamentals}"
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < CACHE_TTL_S:
            return cached[1]

        g = Grounding(ticker=ticker.upper(), as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        try:
            bars, src = self.provider.daily_bars(ticker)
            g.bars = bars
            g.sources.append(src)
        except Exception as exc:  # noqa: BLE001 - failure is data, not a crash
            g.missing.append("price")
            g.errors.append(f"{self.provider.name}: {exc}")

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

        self._cache[key] = (time.time(), g)
        return g


def intraday_supported() -> bool:
    """Free daily data cannot power an intraday decision. The service states this."""
    return False
