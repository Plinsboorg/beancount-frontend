"""Exercise set_project / split_txn / project export against a ledger COPY.

Run against a scratch copy (never the real ledger — it git-commits writes):
  LEDGER=/path/to/copy/main.beancount python test_split_project.py
"""

from __future__ import annotations

import os
from decimal import Decimal

from beanledger import BeanLedger, classify_legs, leg_project

LEDGER = os.environ["LEDGER"]


def row_by_id(diff: dict, rid: str) -> dict | None:
    return next((r for r in diff["transaction"] if r["id"] == rid), None)


def main():
    bl = BeanLedger(LEDGER)
    assert not bl.errors(), f"ledger invalid before test: {bl.errors()}"

    # pick a single-leg expense txn big enough to split
    victim = None
    for t in bl._txns():
        _fund, cats = classify_legs(t)
        if len(cats) != 1 or not t.meta.get("id"):
            continue
        p = cats[0][1]
        if (p.account.startswith("Expenses:") and p.units.number > 2
                and not p.cost and not p.price and not leg_project(t, p)):
            victim = t
            break
    assert victim, "no suitable expense txn found"
    tid = victim.meta["id"]
    total = classify_legs(victim)[1][0][1].units.number
    print(f"victim: {tid} {victim.payee} {total}")

    # 1. set a project on the single leg
    r = bl.set_project(tid, "Test Project Alpha")
    assert r["ok"] and r["project"] == "test-project-alpha", r
    diff = bl.build_zm_diff()
    row = row_by_id(diff, tid)
    assert row["tag"][1:] == ["#test-project-alpha"], row["tag"]
    assert any(t["id"] == "#test-project-alpha" for t in diff["tag"])
    print("1. set_project OK")

    # 2. split into two legs with different categories + projects
    a = total - Decimal("1.00")
    legs = [
        {"amount": float(a), "category": "Salaries", "project": "proj-a"},
        {"amount": 1.00, "category": "Hosting", "project": "proj-b"},
    ]
    r = bl.split_txn(tid, legs)
    assert r["ok"] and not r["errors"], r
    diff = bl.build_zm_diff()
    r0, r1 = row_by_id(diff, f"{tid}~0"), row_by_id(diff, f"{tid}~1")
    assert row_by_id(diff, tid) is None, "unsplit row still present"
    assert r0 and r1, "split rows missing"
    assert Decimal(str(r0["outcome"])) == a and r0["tag"] == ["Expenses:Salaries", "#proj-a"], r0
    assert Decimal(str(r1["outcome"])) == 1 and r1["tag"] == ["Expenses:Hosting", "#proj-b"], r1
    print("2. split OK")

    # 3. re-target one leg's project
    r = bl.set_project(f"{tid}~1", "proj-c")
    assert r["ok"], r
    diff = bl.build_zm_diff()
    assert row_by_id(diff, f"{tid}~1")["tag"][1] == "#proj-c"
    assert row_by_id(diff, f"{tid}~0")["tag"][1] == "#proj-a", "sibling leg was touched"
    print("3. per-leg set_project OK")

    # 4. sum mismatch must be rejected
    try:
        bl.split_txn(tid, [{"amount": 1.00, "category": "Hosting"}])
        raise AssertionError("sum mismatch accepted")
    except ValueError:
        print("4. sum-mismatch rejected OK")

    # 5. protection: entity-scope categorize must skip a vrs-reimbursed leg
    r = bl.set_project(f"{tid}~1", "vrs-reimbursed")
    assert r["ok"], r
    entity = classify_legs(bl._txn_by_id(tid))[1][1][1].account.split(":")[-1]
    bl.categorize("entity", entity, "Recat-Test")
    p1 = classify_legs(bl._txn_by_id(tid))[1][1][1]
    assert "Recat-Test" not in p1.account, f"protected leg was recategorized: {p1.account}"
    print("5. protection OK")

    # 6. merge back to a single leg, clear project
    r = bl.split_txn(tid, [{"amount": float(total), "category": "Salaries"}])
    assert r["ok"], r
    r = bl.set_project(tid, None)
    assert r["ok"] and r["project"] is None, r
    diff = bl.build_zm_diff()
    row = row_by_id(diff, tid)
    assert row and row["tag"] == ["Expenses:Salaries"], row
    print("6. merge-back + clear OK")

    assert not bl.errors(), bl.errors()
    log = bl.git("log", "--oneline", "-8").stdout
    print("git log:\n" + log)
    print("ALL OK")


if __name__ == "__main__":
    main()
