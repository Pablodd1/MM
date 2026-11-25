"""Apply the sizing rewrite to mmdesk/rules.py (run once)."""

from pathlib import Path

p = Path("mmdesk/rules.py")
s = p.read_text(encoding="utf-8")

ACCOUNT_OLD = '''    commission_per_share: float = 0.0  # round-trip cost per share, user/broker supplied
    slippage_bps: float = 0.0          # per side, in basis points of price'''
ACCOUNT_NEW = '''    commission_per_share: float = 0.0  # round-trip cost per share, user/broker supplied
    slippage_bps: float = 0.0          # per side, in basis points of price
    fixed_fee_per_order: float = 0.0   # flat fee charged once per order
    existing_open_risk: float = 0.0    # currency already at risk in open positions
    allow_fractional_shares: bool = False'''
assert s.count(ACCOUNT_OLD) == 1
s = s.replace(ACCOUNT_OLD, ACCOUNT_NEW)

GUARD_OLD = '''        if not 0 <= self.slippage_bps <= 200:
            raise ValueError("slippage_bps must be in [0, 200]")'''
GUARD_NEW = '''        if not 0 <= self.slippage_bps <= 200:
            raise ValueError("slippage_bps must be in [0, 200]")
        if self.fixed_fee_per_order < 0:
            raise ValueError("fixed_fee_per_order cannot be negative")
        if self.existing_open_risk < 0:
            raise ValueError("existing_open_risk cannot be negative")'''
assert s.count(GUARD_OLD) == 1
s = s.replace(GUARD_OLD, GUARD_NEW)

NEW = Path("scripts/_size_position.py").read_text(encoding="utf-8")
start = s.index("def size_position(setup: Setup, account: Account) -> dict:")
end = s.index("def exit_signals(")
s = s[:start] + NEW + s[end:]
p.write_text(s, encoding="utf-8")
print("rewrote size_position:", "available_after_open_risk" in s)
