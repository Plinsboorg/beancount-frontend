"""One-off migration: `#vrs-reimbursed` transactions get `project: "vrs-reimbursed"`
entry-level metadata, so the Projects tab keeps grouping them now that beancount
#tags are no longer exported. The tag itself stays in the file (the entity-scope
protection rules still honor it, and it documents history).

  LEDGER=/path/to/main.beancount python migrate_tags_to_projects.py
"""

from __future__ import annotations

import os

from fava.core.file import get_entry_slice, save_entry_slice
from fava.beans.str import to_string

from beanledger import BeanLedger

TAG = "vrs-reimbursed"


def main():
    bl = BeanLedger(os.environ["LEDGER"])
    assert not bl.errors(), f"ledger invalid before migration: {bl.errors()}"

    targets = [
        t for t in bl._txns()
        if TAG in (t.tags or set()) and not t.meta.get("project")
    ]
    print(f"{len(targets)} transactions to migrate")

    # bottom-up per file so line numbers of not-yet-edited entries stay valid
    targets.sort(
        key=lambda t: (str(t.meta.get("filename")), int(t.meta.get("lineno", 0))),
        reverse=True,
    )
    for t in targets:
        new_entry = t._replace(meta={**t.meta, "project": TAG})
        _slice, sha = get_entry_slice(t)
        save_entry_slice(t, to_string(new_entry, 33, 2), sha)

    bl.ledger.load_file()
    assert not bl.errors(), f"ledger INVALID after migration: {bl.errors()}"

    diff = bl.build_zm_diff()
    n = sum(1 for r in diff["transaction"] if f"#{TAG}" in (r["tag"] or []))
    print(f"export now shows {n} rows in project #{TAG}")

    bl.git_snapshot(f"migrate: entry-level project metadata for {len(targets)} {TAG} txns")
    print("OK, ledger valid, git snapshot taken")


if __name__ == "__main__":
    main()
