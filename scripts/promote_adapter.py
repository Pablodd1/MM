"""Promote a trained tenant adapter — or refuse it out loud.

Training scripts are the party with an incentive to call a run a success. The gate lives in
the registry (``mmdesk.tenants.activate_adapter``); this script reports its verdict and adds
no threshold of its own, so "we trained something" and "it is allowed to answer" stay separate
facts and there is one bar to change, not two.

    python scripts/promote_adapter.py --tenant acme
    python scripts/promote_adapter.py --tenant acme --show
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmdesk import tenants  # noqa: E402


def _shown(adapter: dict) -> dict:
    """The recorded candidate without anything that opens a door."""
    return {k: ("<not shown>" if k == "key" and v else v) for k, v in adapter.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--show", action="store_true", help="print the recorded candidate and exit")
    args = ap.parse_args()

    t = tenants.get(args.tenant)
    if t is None:
        print(f"unknown tenant: {args.tenant}")
        return 2

    candidate = t.pending_candidate or {}
    if candidate:
        ev = candidate.get("eval") or {}
        print(f"candidate: agreement={ev.get('stance_agreement')} "
              f"leakage_free={ev.get('leakage_free')} digit_free={ev.get('digit_free_rate')} "
              f"cases={ev.get('eval_cases')}")
    if args.show or not candidate:
        print(json.dumps({"tenant": t.id, "candidate": _shown(candidate),
                          "active": t.active_adapter is not None}, indent=2))
        return 0 if candidate else 2

    try:
        activated = tenants.activate_adapter(args.tenant)
    except ValueError as exc:
        print(f"REFUSED by the registry: {exc}")
        print("The adapter stays a candidate; the app keeps using the hosted model or the template.")
        return 1
    print(json.dumps({"tenant": args.tenant, "status": activated.get("status"),
                      "activated_at": activated.get("activated_at")}, indent=2))
    print("Layer 2 for this tenant now answers with its own adapter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
