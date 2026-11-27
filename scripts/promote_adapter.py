"""Promote a trained tenant adapter — or refuse it out loud.

Training scripts are the party with an incentive to call a run a success. The gate lives in the
registry (``mmdesk.tenants.activate_adapter``) and this script only reports its verdict, so
"we trained something" and "it is allowed to answer" stay separate facts.

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

BAR = 0.85


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--show", action="store_true", help="print the recorded candidate and exit")
    args = ap.parse_args()

    t = tenants.get(args.tenant)
    if t is None:
        print(f"unknown tenant: {args.tenant}")
        return 2

    adapter = t.adapter or {}
    if args.show or not adapter:
        print(json.dumps({"tenant": t.id, "adapter": adapter,
                          "gate": {"stance_agreement_min": BAR, "requires_leakage_free": True}},
                         indent=2))
        return 0 if adapter else 2

    ev = adapter.get("eval") or {}
    agreement = ev.get("stance_agreement")
    leak_free = bool(ev.get("leakage_free"))
    print(f"candidate: agreement={agreement} leakage_free={leak_free} "
          f"digit_free={ev.get('digit_free_rate')} cases={ev.get('eval_cases')}")

    if agreement is None or agreement < BAR or not leak_free:
        print(f"REFUSED: needs agreement >= {BAR} and a leakage-free evaluation. "
              "The adapter stays a candidate; the app keeps using retrieval or the hosted model.")
        return 1

    activated = tenants.activate_adapter(args.tenant)
    print(json.dumps({"tenant": args.tenant, "status": activated.get("status"),
                      "activated_at": activated.get("activated_at")}, indent=2))
    print("Layer 2 for this tenant now answers with its own adapter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
