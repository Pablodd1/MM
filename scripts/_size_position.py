def _floor_shares(value: Decimal) -> Decimal:
    """Always round down. Rounding to nearest is how a risk budget gets breached."""
    if value >= 1:
        return value.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def size_position(setup: Setup, account: Account) -> dict:
    """Shares from the risk budget, with every cap and cost made explicit.

    Invariant enforced below: the maximum plausible loss at invalidation — including
    entry/exit slippage, round-trip commission and a flat fee — never exceeds the
    stated risk budget minus risk already committed to open positions.

        budget        = equity * risk_pct          (intraday: also capped at MAX_DAY_RISK_PCT)
        available     = budget - existing_open_risk - fixed_fee_per_order
        per_share     = (entry_adj - stop_adj) + commission_per_share
        by_risk       = floor(available / per_share)
        by_conviction = floor(equity * max_pct(score) / entry_adj)
        by_cash       = floor(available_cash / entry_adj)
        shares        = min(by_risk, by_conviction, by_cash)

    If the result is below the smallest tradeable size, the answer is zero shares with a
    reason — never a forced single share that risks more than the user asked for.

    Arithmetic is Decimal end to end: binary floats turn 0.999 into 0.9989999999 and make
    the budget comparison unreliable exactly at the boundary.
    """
    score = score_10(setup.component_scores)
    pct = position_pct(score, setup.alert)
    d_equity = Decimal(str(account.equity))
    d_price = Decimal(str(setup.price))
    d_stop = Decimal(str(setup.invalidation))

    if pct == 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "reason": f"no new exposure: alert={setup.alert}, score={score}",
            "citations": [CITATIONS["position_by_score"], CITATIONS["tiered_size"]],
        }

    slip = Decimal(str(account.slippage_bps)) / Decimal(10000)
    d_comm = Decimal(str(account.commission_per_share))
    d_fixed = Decimal(str(account.fixed_fee_per_order))
    d_open = Decimal(str(account.existing_open_risk))

    entry_adj = d_price * (1 + slip)
    exit_adj = d_stop * (1 - slip)
    per_share = (entry_adj - exit_adj) + d_comm

    budget = d_equity * Decimal(str(account.risk_pct_per_trade))
    if setup.mode == "intraday":
        budget = min(budget, d_equity * Decimal(str(MAX_DAY_RISK_PCT)))

    available = budget - d_open - d_fixed
    notes: list[str] = []

    if available <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": (
                "risk budget already exhausted by open positions and fees: "
                f"{float(d_open):.2f} committed vs {float(budget):.2f} budget"
            ),
            "citations": [CITATIONS["position_by_score"]],
        }

    if per_share <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": "stop is at or above entry after costs: no definable risk per share",
            "citations": [CITATIONS["position_by_score"]],
        }

    by_risk = _floor_shares(available / per_share)
    by_conviction = _floor_shares((d_equity * Decimal(str(pct))) / entry_adj)
    by_cash = None
    if account.cash_pct is not None and account.min_cash_pct > 0:
        spendable = max(
            Decimal(0),
            (Decimal(str(account.cash_pct)) - Decimal(str(account.min_cash_pct))) * d_equity,
        )
        by_cash = _floor_shares(spendable / entry_adj)

    candidates = {"risk": by_risk, "conviction": by_conviction}
    if by_cash is not None:
        candidates["cash"] = by_cash
    capped_by = min(candidates, key=lambda k: candidates[k])
    shares = candidates[capped_by]

    if not account.allow_fractional_shares:
        shares = shares.quantize(Decimal("1"), rounding=ROUND_DOWN)

    if shares <= 0:
        return {
            "shares": 0, "notional": 0.0, "pct_of_equity": 0.0, "score_10": score,
            "risk_at_stop": 0.0, "risk_budget": round(float(budget), 2),
            "reason": (
                "stop too wide for the risk budget: one share risks "
                f"{float(per_share):.2f} against {float(available):.2f} available "
                f"(limited by {capped_by})"
            ),
            "costs": {
                "slippage_bps_per_side": account.slippage_bps,
                "commission_per_share_round_trip": account.commission_per_share,
                "fixed_fee_per_order": account.fixed_fee_per_order,
            },
            "capped_by": capped_by,
            "citations": [CITATIONS["position_by_score"]],
        }

    notional = shares * entry_adj
    risk_at_stop = shares * per_share + d_fixed

    if capped_by == "cash":
        notes.append("cash floor limited the size")
    if account.existing_open_risk:
        notes.append(f"existing open risk {float(d_open):.2f} deducted before sizing")

    # The promise: never above the stated budget, by construction.
    assert risk_at_stop <= budget + Decimal("0.01"), "sizing exceeded the risk budget"

    return {
        "shares": int(shares) if not account.allow_fractional_shares else float(shares),
        "notional": round(float(notional), 2),
        "pct_of_equity": round(float(notional / d_equity), 4),
        "score_10": score,
        "risk_at_stop": round(float(risk_at_stop), 2),
        "risk_pct_of_equity": round(float(risk_at_stop / d_equity), 4),
        "risk_budget": round(float(budget), 2),
        "available_after_open_risk": round(float(available), 2),
        "costs": {
            "slippage_bps_per_side": account.slippage_bps,
            "commission_per_share_round_trip": account.commission_per_share,
            "fixed_fee_per_order": account.fixed_fee_per_order,
            "total_estimated": round(float(
                shares * d_comm
                + shares * (entry_adj - d_price)
                + shares * (d_stop - exit_adj)
                + d_fixed
            ), 2),
        },
        "effective_entry": round(float(entry_adj), 4),
        "effective_stop": round(float(exit_adj), 4),
        "reward_at_target": round(float(shares * (Decimal(str(setup.first_target)) - entry_adj)), 2),
        "reward_risk": round(float((Decimal(str(setup.first_target)) - entry_adj) / per_share), 2),
        "capped_by": capped_by,
        "notes": notes,
        "citations": [CITATIONS["position_by_score"], CITATIONS["tiered_size"]],
    }


