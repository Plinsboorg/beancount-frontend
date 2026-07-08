# beancount-frontend workspace

Reworking **Zerro** (a React budgeting app) into a **categorization + analytics frontend for a
Beancount ledger**. Prepared by the planning agent for hand-off to an implementation agent.

## ✅ STATUS (2026-07-03): built & deployed
The app is **live at http://10.14.88.72:5113/** (LAN), running as a systemd user service
(`zerro-bean`, linger enabled). Working: Transactions (per-txn categorize), **Merchants**
(categorize-by-merchant with bulk durable edits + future-import rules), Stats, Review —
all against the local dev copy of the Plinsburg ledger (never touches production).

- Backend: `service/` — FastAPI importing `fava.core` (see `service/README.md`).
- Frontend: `repos/zerro`, branch **`beancount`** (rebuild: `pnpm run build`).
- Every categorize is a durable, sha256-guarded ledger edit, validated and git-committed
  in `ledger-plinsburg/plinsburg-ledger/` (git-initialized, full history since import).
- All 551 transactions carry stable `id:` metadata now (SPEC O2, `service/add_ids.py`).
- Not yet done: Phase 5 (beangulp import loop), Phase 6 (deploy beside prod Fava).

## Start here
1. **`spec/SPEC.md`** — the implementation spec. Read it first. Read §1 (goal + the pivotal
   assumption A1) and §11 (open decisions) before writing code.
2. **`spec/CATEGORIZATION_GUI_BRIEF.md`** — Dennis's real ledger (Plinsburg Tech). Note: its
   "regenerate everything" model was a temporary bootstrap — superseded by SPEC §1.3.
3. **`analysis/`** — deep dives backing the spec:
   - `zerro.md` — Zerro architecture, budget engine, keep/adapt/rewrite map.
   - `beancount.md` — Beancount v3.2 data model + safe-write pattern.
   - `fava.md` — Fava's `FavaLedger` + JSON API + the reusable sha256 writer.

## Layout
```
repos/zerro       React frontend to rework (deps installed, builds clean)
repos/beancount   Beancount 3.2.3 engine (reference)
repos/fava        Fava (reference + the code we reuse as a library)
analysis/         the three analysis docs
spec/             SPEC.md + the brief
ledger/           sample.beancount smoke-test ledger
ledger-plinsburg/plinsburg-ledger/   ← Dennis's REAL ledger (local dev copy, see below)
.venv/            python 3.14 venv: beancount, fava, beanquery
```

## Real ledger (dev data)
Dennis's actual Plinsburg Tech ledger has been imported here as the **local dev data source**:
`ledger-plinsburg/plinsburg-ledger/` (extracted from a tarball he provided). It contains the full
pipeline + seed + all `.beancount` files. It **loads clean: 779 entries, 551 transactions, 0
errors, operating currency PLN**.

- This is a **local copy** — completely separate from production (`root@192.168.1.115:/opt/fava/beancount/`).
  Develop and preview against this copy; nothing here touches the real host.
- **View it in Fava** (stock, read-only — the new Zerro UI is not built yet):
  ```
  cd ~/beancount-frontend/ledger-plinsburg/plinsburg-ledger
  ~/beancount-frontend/.venv/bin/fava -p 5112 -H 0.0.0.0 main.beancount
  ```
  → http://127.0.0.1:5112/  (LAN: http://10.14.88.72:5112/ ; lands on `.../income_statement/`)
- Use this ledger to build/preview the Zerro categorization UI against real data (per SPEC §12).

## Environment (verified 2026-07-03)
- Node v24.18.0 (`~/.nvm/versions/node/v24.18.0/bin` — add to PATH), pnpm 10.33.2.
- Zerro: `cd repos/zerro && pnpm dev` (port 3000); `pnpm run build` passes.
- Python: `.venv/bin/{python,fava,bean-query}`; loads/queries/serves `ledger/sample.beancount`.
- See SPEC.md §10 for the full runbook and passing smoke tests.

## The one-line summary
Zerro's UI + a thin FastAPI service that imports `fava.core` for durable, sha256-safe writes to a
persistent `.beancount` ledger. Categories = `Expenses:`/`Income:` accounts; categorizing = durable
edit of the category posting. **No envelope budgeting.**
