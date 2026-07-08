# beancount-frontend

A **categorization + analytics UI for a [Beancount](https://beancount.github.io/) ledger**,
built by reworking [Zerro](https://github.com/ardov/zerro) (a React budgeting app) onto a thin
[FastAPI](https://fastapi.tiangolo.com/) backend that reuses [Fava](https://beancount.github.io/fava/)'s
core for durable, safe ledger writes.

The idea: keep Zerro's fast, polished transaction UI, but point it at a plain-text Beancount
ledger instead of a cloud budgeting service. Categorizing a transaction becomes a durable,
validated edit of the category posting in your `.beancount` files — no envelope budgeting, no
external sync.

## What it does
- **Transactions** — review and categorize transactions one at a time.
- **Merchants** — categorize by merchant/payee in bulk, with an "uncategorized" filter, and
  optionally persist a rule for future imports.
- **Stats / Review** — spending analytics over the ledger.
- Every categorize is a **durable, sha256-guarded ledger edit**: the backend rewrites the
  category posting, auto-inserts any missing `open` directives, reloads to validate, and
  commits the change to the ledger's git history.

Categories are just `Expenses:` / `Income:` accounts — categorizing is a durable edit of the
category posting. There is **no budgeting layer**; Zerro's envelope/budget engine is present in
the frontend but disabled (routes and nav removed).

## Architecture
```
┌────────────────────┐     TDiff (Zenmoney shape)     ┌──────────────────────┐
│  Zerro frontend    │  ───────────────────────────▶  │  FastAPI service     │
│  (React SPA)       │  ◀───────────────────────────  │  imports fava.core   │
│  fork: our branch  │     categorize / delete        │  sha256-safe writes  │
└────────────────────┘                                └──────────┬───────────┘
                                                                 │
                                                       ┌─────────▼──────────┐
                                                       │  .beancount ledger │
                                                       │  (git-versioned)   │
                                                       └────────────────────┘
```
The backend presents the ledger in the Zenmoney `TDiff` shape the frontend already understands
(accounts, tags-as-categories, merchants-as-entities, 2-leg transactions), so the UI needed only
a data-provider swap rather than a rewrite.

## Repositories
- **This repo** — the backend service, spec, and analysis docs.
- **Frontend** — [`Plinsboorg/zerro` @ branch `beancount`](https://github.com/Plinsboorg/zerro/tree/beancount)
  (a fork of [`ardov/zerro`](https://github.com/ardov/zerro)).

To reproduce the exact frontend that pairs with this backend, clone that branch:
```
git clone -b beancount https://github.com/Plinsboorg/zerro.git
```

## Layout
```
service/    FastAPI backend (see service/README.md for the endpoints)
spec/       SPEC.md — the implementation spec + categorization brief
analysis/   deep dives: Zerro architecture, Beancount data model, Fava's safe-write pattern
ledger/     sample.beancount — a tiny smoke-test ledger
```

## Running it
Requirements: Python 3.12+, Node ≥ 20 + pnpm, and a Beancount ledger of your own.

1. **Backend** — create a venv and install `fastapi`, `uvicorn`, `fava`, `beancount`, then:
   ```
   LEDGER=/path/to/your/main.beancount STATIC_DIR=/path/to/zerro/dist \
     python -m uvicorn app:app --app-dir service --host 0.0.0.0 --port 5113
   ```
2. **Frontend** — clone the [zerro fork](https://github.com/Plinsboorg/zerro) (branch `beancount`),
   `pnpm install && pnpm run build`, and point `STATIC_DIR` at its `dist/`. For development,
   `pnpm dev` proxies `/api` to the backend.
3. Open the app and start categorizing. Writes go straight to your ledger and are git-committed.

See `service/README.md` for the full API and `spec/SPEC.md` for design details.

## Status
Working: Transactions, Merchants, Stats, Review — all writing durable, validated edits back to
the ledger. Planned: a beangulp-based bank-statement import loop that consumes the merchant
rules the UI produces.
