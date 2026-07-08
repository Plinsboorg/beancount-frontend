"""One-time migration: add stable `id:` metadata to every transaction (SPEC §8.5, O2).

Idempotent — transactions that already have an `id:` are skipped. Ids are 8-hex
random tokens. Run bean-check (via loader) after to confirm the ledger still loads.

Usage: .venv/bin/python service/add_ids.py <path/to/main.beancount>
"""

import sys
import uuid
from collections import defaultdict
from pathlib import Path

from beancount import loader
from beancount.core.data import Transaction


def main(main_file: str) -> None:
    entries, errors, _ = loader.load_file(main_file)
    assert not errors, f"ledger has {len(errors)} errors, aborting"

    by_file: dict[str, list[int]] = defaultdict(list)
    for e in entries:
        if isinstance(e, Transaction) and "id" not in e.meta:
            by_file[e.meta["filename"]].append(e.meta["lineno"])

    total = 0
    for fname, linenos in by_file.items():
        path = Path(fname)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for lineno in sorted(linenos, reverse=True):
            lines.insert(lineno, f'  id: "{uuid.uuid4().hex[:8]}"\n')
            total += 1
        path.write_text("".join(lines), encoding="utf-8")
        print(f"{path.name}: +{len(linenos)} ids")

    entries, errors, _ = loader.load_file(main_file)
    assert not errors, f"MIGRATION BROKE THE LEDGER: {errors[:3]}"
    n = sum(1 for e in entries if isinstance(e, Transaction) and "id" in e.meta)
    print(f"OK: {total} ids added, {n} transactions carry ids, 0 errors")


if __name__ == "__main__":
    main(sys.argv[1])
