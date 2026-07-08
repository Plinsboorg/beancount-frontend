# Spec: A Zerro-based React frontend for a Beancount ledger

**Project codename:** *(tbd — "zerro-bean" used below as a placeholder)*
**Author:** planning/analysis agent
**Date:** 2026-07-03
**Status:** ready for execution by an implementation agent
**Owner:** Dennis (`dennis@vrs.racing`) — single user, self-hosted

---

## 0. Read-me-first for the implementing agent

This spec is self-contained but backed by three deep-dive analyses you should read before
coding. They contain quoted type definitions, file paths, and endpoint shapes:

| doc | what it covers |
|---|---|
| `../analysis/zerro.md` | Zerro's architecture, data model, budget engine, and the **exact seam to replace**. File-by-file keep/adapt/rewrite breakdown in §5.3. |
| `../analysis/beancount.md` | Beancount v3.2 data model (directives, Posting/Transaction, Decimal amounts), loading/realization, and safe text-write pattern. |
| `../analysis/fava.md` | Fava's `FavaLedger` abstraction, its JSON API contract, and the **safe sha256-guarded write machinery** (`fava.core.file`) we reuse. |
| `./CATEGORIZATION_GUI_BRIEF.md` | Dennis's *actual* ledger: the Plinsburg Tech books. Read §2 (account = category model), §4 (transaction identity), §6 (gotchas: multi-currency, diacritics, sign convention, existing overrides). **Note the important scope correction in §1 below — the "generated ledger / regenerate everything" model in that brief was a temporary bootstrap and is NOT the going-forward design.** |

The working environment is already prepared and verified (see §10). All three source repos are
cloned under `../repos/`.

---

## 1. Goal, scope, and the pivotal assumption

### 1.1 What we are building

A beautiful, responsive React web app — **Zerro's UI, re-backed onto a Beancount ledger** —
whose primary jobs are:

1. **Assign categories to transactions** with Zerro-quality UX. This is the headline feature and
   the thing Fava lacks a good UI for. Default editing unit = **per merchant/entity**, with a
   **per-transaction** escape hatch.
2. **Analytics** — Zerro's Stats page and the year-end **Review** report, over the real ledger.
3. **Responsive PWA** — keep Zerro's mobile-friendly, offline-capable shell.

### 1.2 Explicitly OUT of scope

- **Envelope / zero-based budgeting.** Dennis does not want it. The entire Zerro budget engine,
  goals, the "to be budgeted" pool, carryover, and the hidden-store reminder hack are **deleted**,
  not ported. (This removes ~60% of the hard porting work — see `../analysis/zerro.md` §4, §5.)
- Zenmoney integration, OAuth, and the diff-sync protocol — **deleted**.
- Multi-user / hosted SaaS. Single user, self-hosted.
- Investment cost-basis / lot tracking beyond what Beancount computes for read-only display.

### 1.3 The pivotal assumption (A1) — confirm before building

> **A1. The Beancount ledger is the persistent source of truth. Categorization edits are durable
> edits to the ledger files, made through `fava.core`'s safe writer. The Firefly →
> `history.beancount` regeneration pipeline was a one-time bootstrap and is NOT re-run in normal
> operation.**

Consequence: the current `history.beancount` is treated as **frozen-and-now-authored**. Durable
edits to it are safe because nothing regenerates it. If Dennis ever re-runs the Firefly bootstrap,
edits to generated history would be clobbered — folding them back would be a one-time migration
(out of scope here).

This assumption reverses the write model recommended in `CATEGORIZATION_GUI_BRIEF.md` (which was
written for the temporary regenerate-everything system). **Everything downstream depends on A1.**
If A1 is wrong and Dennis wants to keep regenerating, switch the backend write target from
"`fava.core` durable edits" to "write `overrides.json` + trigger `regen.sh`" (the brief's model) —
the frontend contract in §5 is unaffected either way, only the backend's `POST /categorize`
implementation changes. See §11-O1.

### 1.4 Going-forward data lifecycle

```
one-time bootstrap (DONE, not repeated):  Firefly export → seed → convert.py → history.beancount
                                                                          │  (frozen, now authored)
steady state:
   bank statement (CSV/MT940) ──importer(beangulp)──▶ appends txns to ongoing.beancount
                                                        (may pre-assign category via merchant rules)
   Dennis in the new app ──recategorize──▶ durable edit to the category posting's account
                                             (fava.core safe write) ──▶ reload ──▶ analytics update
```

---

## 2. Target architecture

```
   browser
     │  static assets (PWA)
     ▼
   Zerro React UI  ──────────────  clean JSON over HTTP  ──────────────┐
   (this repo, reworked)                                               │
                                                                       ▼
                                                     ┌─────────────────────────────────┐
                                                     │  zerro-bean-service (NEW)         │
                                                     │  FastAPI (Python)                 │
                                                     │   • GET ledger/transactions/...   │
                                                     │   • POST categorize (durable)     │
                                                     │   • add/edit/delete txn           │
                                                     │      imports ↓                    │
                                                     │   fava.core.FavaLedger            │  ← reuse
                                                     │   fava.core.file (sha256 writes)  │  ← reuse
                                                     │   fava.serialisation / fava.beans │  ← reuse
                                                     │      loads ↓                      │
                                                     │   beancount (engine)              │
                                                     └─────────────────────────────────┘
                                                                       │
                                                                       ▼
                                                     ledger/*.beancount  (source of truth, git-versioned)
                                                       main.beancount → prices + history + ongoing + manual

   (unchanged, parallel) existing Fava at http://192.168.1.115:2502  — read-only analytics/audit
```

**Why this shape** (full reasoning in `../analysis/fava.md` §6, recommendation "B"):
- Beancount is a Python library, not a server — a browser cannot run it, and cannot touch
  server-side ledger files without a backend process. A backend is mandatory in this topology.
- We reuse Fava's *code* (loading, reload-on-change, balances, and especially the **sha256
  optimistic-concurrency safe writer**) as a library, but expose a **small purpose-built JSON API**
  shaped for Zerro instead of running stock Fava (whose API is slug-namespaced, partly HTML, and
  has no clean categorize endpoint).
- The existing Fava install stays valid and untouched as an independent report/audit view.

**Where the service runs:** on the host that owns the ledger files (currently the Fava Docker CT
`root@192.168.1.115`, ledger dir `/opt/fava/beancount/`, per the brief §7). Simplest: add the new
service as a second process/container beside Fava on that host, pointed at the same
`/data/main.beancount`. Auth: reuse the existing Caddy basic-auth in front (single user).

---

## 3. Domain mapping — Zerro concepts ↔ Beancount

This is the heart of the rework. Zerro's UI speaks in accounts/tags/transactions; we map those to
Beancount. Detailed Zerro types in `../analysis/zerro.md` §1; Beancount types in
`../analysis/beancount.md` §1.

| Zerro concept | Beancount concept | Notes |
|---|---|---|
| **Category / tag** | `Expenses:*` and `Income:*` account (the middle segment(s)) | The account **tree is the category tree**. Categories can be multi-level (`Sales-Revenue:Electronics-Consulting`). See brief §2. |
| **Account** (cash/card/wallet) | `Assets:*` / `Liabilities:*` account | The 3 real accounts today: `Assets:Bank:PKO:{Firmowy(PLN),USD,EUR}` (brief §4). |
| **Merchant / payee** | The **entity** = account leaf (consolidated counterparty) and/or the txn `payee` | Default categorization unit. ~85 non-VRS merchants (brief §2). |
| **Transaction** | `Transaction` with N `Posting`s | Zerro's 2-leg income/outcome model is a subset — see §8.1 for the N-posting bridge. |
| **"Assign category to txn"** | Rewrite the account of the **non-Asset/Liability posting** | For a 2-posting expense: change `Expenses:FIXME` → `Expenses:Food:Groceries`. Durable via fava.core. |
| **Transfer** | Both legs are Assets/Liabilities | No category. Multi-currency transfers use `@@` cost (brief §4). |
| **Income** | posting into `Income:*` (credit-normal → negative amount) | Sign convention: income legs negative, expenses positive (brief §6). |
| **Currency / instrument** | Beancount commodity (`PLN`,`USD`,`EUR`) + `Price` directives | Home/operating currency = **PLN**. FX rates from `prices.beancount` (NBP). |
| **Stable txn id** | recommended: `id:` metadata (uuid or the old `jid`); transient: Fava `entry_hash` | See §8.4. |
| ~~Envelope / budget / goal~~ | — | **Deleted.** |

### 3.1 "Category" precisely defined

For a transaction, the **category** is the account of the posting that is NOT an Asset or
Liability, i.e. the `Income:*` or `Expenses:*` leg:
- Exactly one such leg → single category (the common case).
- Zero such legs (both Assets/Liabilities) → a **transfer**, no category.
- More than one such leg → a **split** (e.g. the 19 SmartyCo income splits, brief §6). Handle per
  §8.2 — display all category legs; editing targets a chosen leg.

---

## 4. The categorization UX (headline feature)

Design the flow around Dennis's real need (brief §5.6): *"filter out anything already in
VRS-Reimbursed, categorize the rest,"* editing **per-merchant by default**.

### 4.1 Two entry points

1. **Categorize-by-merchant view (default).** Group categorizable transactions by entity/payee.
   Each row = one merchant: current dominant category, txn count, total amount, currencies.
   Clicking a merchant lets you assign a category that **bulk-applies to all its transactions**
   (durable edits) and optionally **saves a `merchant → category` importer rule** for future
   imports. Reuse Zerro's `4-features/bulkActions` and merchant/DebtorList patterns
   (`../analysis/zerro.md` §1.6).
2. **Per-transaction view.** Zerro's Transactions page (`src/2-pages/Transactions`), used for
   exceptions — a single transaction whose category differs from its merchant's default. Writes a
   per-transaction durable edit.

### 4.2 Category picker

- Autocomplete against existing categories (the 25 in brief §2) **and the full `Expenses:`/
  `Income:` account tree**.
- Allow **new** categories (free text) and **multi-level** paths (`A:B:C`).
- Normalize human text → valid Beancount account components (e.g. `Software & Subscriptions` →
  `Software-Subscriptions`). Use the same slugification the pipeline uses; transliterate Polish
  diacritics (brief §6 — `ą ć ę ł ń ó ś ź ż`, `URZĄD`→`URZAD`).

### 4.3 Filters (all required — brief §5.6)

by category · by tag (esp. **exclude `#vrs-reimbursed`**) · by merchant/entity · by date range ·
by amount · by currency · **"uncategorized / Other" quick filter** (category is `Other`,
`Expenses:FIXME`, `Expenses:Unknown`, or a configurable placeholder set).

### 4.4 Must-not-clobber (brief §6)

Preserve existing curated data: 76 txns tagged `#vrs-reimbursed` force-categorized to
`Expenses:VRS-Reimbursed`; 19 SmartyCo income splits; placeholder/return fixes. Because A1 makes
these durable ledger facts, the app simply reads and respects them; it must not silently overwrite
tags or split structure when recategorizing.

### 4.5 Write feedback

Every write reloads the ledger and runs validation (`bean-check` semantics via
`FavaLedger.load_errors`). Surface success / validation errors inline (a bad edit can unbalance a
split). The ledger dir is git-versioned (brief §1) so a snapshot/rollback path exists.

---

## 5. Backend service — the JSON contract (NEW component)

A small **FastAPI** app (`zerro-bean-service`) that instantiates one `FavaLedger(path)` and exposes
the endpoints below. Reuse, do not reimplement: `FavaLedger` (load/reload/balances/filter),
`fava.core.file` (`insert_entries`, `save_entry_slice`, `delete_entry_slice` — all sha256-guarded),
`fava.serialisation` + `fava.beans.str.to_string` (entry↔JSON↔text). See `../analysis/fava.md`
§1–3 for exact method signatures and the safety guarantees.

Conventions: plain JSON (no `{data,mtime}` wrapper unless useful), one ledger (no `<slug>`),
ISO dates, amounts as `{number: string, currency: string}` (string to preserve Decimal precision —
**never floats over the wire**), `mtime`/`sha256` echoed where writes need optimistic concurrency.

### 5.1 Read endpoints

| Method + path | Params | Returns |
|---|---|---|
| `GET /api/ledger` | – | Seed blob: `operating_currency` (`"PLN"`), `commodities`, **account tree** (typed: asset/liability/income/expense/equity, with open/close), category list, entity/payee list, tag list, beancount options, `load_errors`. Analogous to Fava `ledger_data` (`../analysis/fava.md` §2.2). |
| `GET /api/transactions` | filters (see §4.3), pagination | Normalized transaction list — shape in §5.3. This single feed powers the Transactions page, the categorize-by-merchant view, Stats, and Review. |
| `GET /api/entities` | filters | Merchants/entities with `{entity, dominantCategory, txnCount, totals: [{currency,number}], side}` for the per-merchant view. |
| `GET /api/categories` | – | Existing categories + full `Expenses:`/`Income:` subtree for autocomplete. |
| `GET /api/prices` | – | FX rates (from `Price` directives / `prices.beancount`) so the frontend can convert multi-currency for stats. `{ [date]: { [pair]: number } }` or Fava-style commodity pairs. |
| `GET /api/balances` | filters, interval | (optional) Account tree with balances if we prefer server-computed over client-computed. See §6. |
| `GET /api/validate` | – | `{ ok: bool, errors: [{message, source}] }` (current `load_errors`). |
| `GET /api/changed` | – | `{ changed: bool, mtime }` — for reload polling (Fava watcher backs this). |

### 5.2 Write endpoints (durable, via `fava.core.file`)

| Method + path | Body | Effect |
|---|---|---|
| `POST /api/categorize` | `{ scope: "txn"\|"entity", target: <id or entity>, category: string, applyToFuture?: bool, sha256?: string }` | **The headline write.** `scope:"txn"` → rewrite the category posting's account on that one transaction (sha256-guarded `save_entry_slice`). `scope:"entity"` → bulk: rewrite the category leg on every existing transaction of that entity, and if `applyToFuture`, persist a `merchant→category` importer rule (§7). Returns `{ updated: Transaction[], validation }`. |
| `POST /api/transactions` | `{ entry: TransactionJSON }` | Add a transaction (`file.insert_entries`). Secondary — for manual entries. |
| `PUT /api/transactions/{id}` | `{ entry, sha256 }` | Edit a whole transaction (source-slice). |
| `DELETE /api/transactions/{id}` | `{ sha256 }` | Delete (source-slice). |
| `POST /api/tags/{id}` | `{ add?: string[], remove?: string[], sha256 }` | Edit a transaction's `#tags` (needed to respect/adjust `#vrs-reimbursed`). |
| `POST /api/import` | multipart statement file + importer id | (Phase 4) run beangulp importer, return extracted entries for review before commit. |

**Concurrency & safety (reuse Fava's, `../analysis/fava.md` §3.3):** every per-entry write carries
the `sha256` of the slice the client read; mismatch → 409 (`ExternallyChangedError`). Writes take a
lock, then `load_file()` reloads and re-derives. Generated/`<...>` entries are not editable.
Preserve the file's newline style. Commit-to-git after successful writes is optional but recommended
(the dir is already a git repo).

### 5.3 Normalized `Transaction` JSON (the frontend's core type)

```jsonc
{
  "id": "e3f1a9…",            // stable ref: id: metadata if present, else Fava entry_hash
  "entry_hash": "e3f1a9…",   // Fava content hash, for the sha256 write round-trip
  "sha256": "…",             // hash of this entry's source slice (edit guard)
  "date": "2026-04-07",
  "flag": "*",
  "payee": "SMARTY CO.",
  "narration": "INVOICE 2026-04",
  "entity": "Smarty-Co",     // consolidated counterparty (category-account leaf)
  "tags": ["vrs-reimbursed"],
  "links": [],
  "postings": [
    { "account": "Assets:Bank:PKO:USD", "amount": {"number":"9317.00","currency":"USD"} },
    { "account": "Income:Sales-Revenue:Electronics-Consulting:Smarty-Co",
      "amount": {"number":"-9317.00","currency":"USD"} }
  ],
  // derived convenience fields for the UI (computed server-side from postings):
  "kind": "income",          // "income" | "expense" | "transfer" | "split"
  "category": "Income:Sales-Revenue:Electronics-Consulting:Smarty-Co", // the categorizable leg's account, null for transfers
  "categoryLegIndex": 1,     // which posting is the category (for scope:"txn" edits); null/[] for transfer; array for splits
  "account": "Assets:Bank:PKO:USD",  // the asset/liability leg
  "amount": {"number":"9317.00","currency":"USD"},  // primary amount (asset-leg magnitude)
  "isSplit": false,
  "editable": true           // false for generated/plugin entries
}
```

Design note: keep the wire type close to Beancount (postings + Decimal-strings), and compute the
convenience fields server-side so the frontend has an unambiguous "which leg is the category."
Do **not** collapse to Zerro's income/outcome float model on the wire — do that mapping in the
frontend provider (§8.1) if we keep Zerro's store shape, or adopt this shape directly (recommended,
§8).

---

## 6. Analytics (Stats + year-end Review)

Zerro already computes Stats and Review **client-side from the transaction list** (its selectors
aggregate transactions by tag/account/month — `../analysis/zerro.md` §3–4). So:

- **Feed those pages the `GET /api/transactions` list + `GET /api/prices` rates.** Categories map to
  accounts, so "spending by category" = grouping by the `category` account; "by account" = the
  asset legs. Multi-currency handled with the price map (home currency PLN).
- Keep `src/2-pages/Stats` and `src/2-pages/Review`; rewire their inputs from the (deleted)
  Zenmoney-derived selectors to the new provider's data. Drop any budget-dependent widgets.
- The existing Fava instance remains for anything deeper; we do not have to reproduce Fava's full
  report surface in v1.

Server-computed balances (`GET /api/balances`, using `FavaLedger`/`realization`) are available as a
fallback/cross-check, but client-side computation keeps Zerro's existing stats code intact. Prefer
client-side to minimize rework; use server balances only where Zerro's backward-from-current
balance logic (`accBalances`, see §8.3) proves awkward.

---

## 7. Merchant rules for future imports (steady-state categorization)

To make "categorize by merchant" persist for **new** transactions without regeneration:

- Maintain a small, durable **`merchant_rules` file** (JSON or a beancount `custom` convention)
  co-located with the ledger, e.g. `{ "Mouser": "Electronics-Components", "Wizz Air": "Travel" }`
  keyed by normalized payee/entity.
- `POST /api/categorize {scope:"entity", applyToFuture:true}` upserts a rule (in addition to the
  durable bulk-edit of existing txns).
- The **beangulp importer** (Phase 4) consults these rules to pre-assign the category account when
  ingesting a bank statement; anything unmatched lands in a placeholder (`Expenses:FIXME`) and
  shows up in the app's "uncategorized" filter for review.

This is the standard Beancount import workflow and replaces the brief's `CAT_OVERRIDE`/`regen`
loop with a durable-ledger equivalent. (Note: `merchant_rules` is a small config, not a store of
categories — the categories themselves live durably in the ledger.)

---

## 8. Frontend rework plan (Zerro repo)

Base: `../repos/zerro` (React 18 + TS + Redux Toolkit + MUI + Vite, Feature-Sliced Design). Full
keep/adapt/rewrite table in `../analysis/zerro.md` §5.3 — summarized and re-scoped here for the
no-budgeting, durable-ledger design.

### 8.0 Recommended refactor depth

Adopt a **neutral, Beancount-shaped domain model** (Decimal-string amounts, N-posting transactions,
accounts-as-categories) rather than impersonating the Zenmoney float/2-leg model. Rationale in
`../analysis/zerro.md` §5.2/§5.4: Beancount's Decimal + N-posting + forward-balances mismatch
Zerro's floats + 2-leg + backward-balances, and since budgeting (the most coupled part) is being
deleted, there's little left that forces us to keep the awkward Zenmoney shape. Concretely: the
new provider produces the §5.3 JSON; a thin `store/data` layer holds it; the surviving
selectors/UI read accounts+categories from it.

### 8.1 DELETE (Zenmoney + budgeting)

- `src/6-shared/api/zenmoney/*`, `src/6-shared/api/zm-adapter/*`, `tokenStorage.ts` — Zenmoney API/OAuth.
- `src/2-pages/Auth/*` — Zenmoney login (replace with nothing or a trivial backend-config screen; auth is Caddy's job).
- `src/2-pages/Budgets/*` — the budgeting page.
- `src/5-entities/envBalances/*`, `src/5-entities/budget/*`, `src/5-entities/goal/*` — the budget/goal engine.
- `src/5-entities/shared/hidden-store/*` and its consumers — the reminder-hack config store.
- `src/5-entities/reminder/*` — only existed for the hidden store / planning.
- `src/4-features/budget/*`, `4-features/envelope/*`, `4-features/moveMoney/*` — budgeting features.
- `src/4-features/authorization.ts`, `4-features/sync.ts`, `4-features/shared/getDataToSave.ts` — Zenmoney sync/auth orchestration.
- The Zenmoney-specific parts of `src/worker/worker.ts` (keep the IndexedDB cache helpers if useful for offline).

### 8.2 BUILD (new)

- `src/6-shared/api/bean-provider/*` — the data provider that talks to `zerro-bean-service`
  (fetch, types matching §5.3, optimistic write + sha256 handling, reload-on-`changed`). This is
  the **new seam** replacing `worker.sync` + `applyServerPatch(TDiff)` (`../analysis/zerro.md` §5.2).
- `src/store/data/*` — slim store holding accounts, categories (account tree), transactions,
  prices. Keep Zerro's optimistic-update pattern (a client-patch action) but with the new payload.
- **Categorize-by-merchant view** (new page/feature) per §4.1 — the primary flow. Reuse
  `4-features/bulkActions` + merchant/DebtorList UI.
- Category picker component per §4.2 (autocomplete over the account tree, allow new/multi-level,
  diacritic-safe slugify).
- **Split handling**: display all category legs; editing targets `categoryLegIndex`; a split whose
  legs you don't want to touch is read-only-respected (§4.4). Full split *editing* can be Phase 3.

### 8.3 ADAPT (reusable logic, rewire inputs)

- `src/2-pages/Transactions/*` — per-transaction categorization UI. Rewire to the provider; map
  the category control to the account of the category leg.
- `src/2-pages/Stats/*`, `src/2-pages/Review/*` — keep the aggregation/visualization; feed from the
  new transaction list + prices (§6); remove budget-dependent widgets.
- `src/5-entities/transaction/*` — `getType`/classification becomes "derive kind from postings"
  (income/expense/transfer/split from account types) instead of Zenmoney income/outcome fields.
- `src/5-entities/account/*`, `currency/*` — populate from the account tree + commodities; drop
  Zenmoney instrument ids (use commodity codes, which Zerro's `TFxCode` already resembles).
- `src/5-entities/accBalances/*` — **invert** balance computation: Beancount is forward-from-postings,
  Zerro was backward-from-current-server-balance (`../analysis/zerro.md` §1.3, §5.4). Or use
  `GET /api/balances` and skip this (§6).
- `src/6-shared/helpers/money/*` — move to Decimal-safe arithmetic (or careful string/Decimal
  handling) since amounts are now Decimal, not floats (`../analysis/beancount.md` §1). Consider a
  small Decimal lib; at minimum, never lose precision on the category-edit path (edits are text, so
  amounts pass through untouched — the risk is only in stats aggregation).

### 8.4 KEEP (as-is)

- The whole UI kit / theme / responsive shell / i18n / icons: `src/6-shared/ui/*`,
  `src/1-app/*`, `src/6-shared/localization/*`, navigation (`src/3-widgets/Navigation` minus Budgets link).
- PWA/service-worker setup (`vite-plugin-pwa`).

### 8.5 Stable transaction identity (§4, brief §4)

Beancount `entry_hash` changes when an entry's text changes (e.g. after recategorizing), so it is
fine as a **write round-trip token** but not as a durable id. For durable references (merchant
rules, import dedup, deep links) recommend adding an `id:` metadata line to each transaction (uuid,
or the old `jid`). The brief author offered to emit `id:` from the pipeline (brief §8) — request it
as a one-time addition to the frozen history; new imports get a fresh uuid. If we skip durable ids,
key merchant rules by normalized payee/entity (still workable).

---

## 9. Key impedance mismatches (plan for these)

(Condensed from `../analysis/zerro.md` §5.4 and `../analysis/beancount.md` §1; re-scoped for this
project.)

1. **N-posting vs 2-leg.** Beancount transactions have N postings; Zerro assumed 2. We adopt the
   N-posting model (§8.0). Simple expense/income = 2 postings (asset + category) — the common case
   and unambiguous for categorization.
2. **Splits (>1 category leg).** ~19 income-split txns. Display all; edit a chosen leg; respect
   existing splits. Don't force Zerro's single-tag model to flatten them.
3. **Decimal vs float.** Amounts are Decimal strings on the wire. The category-edit path is pure
   text (amounts untouched), so precision risk is confined to client-side stats aggregation — use
   Decimal-safe math there.
4. **Forward vs backward balances.** Compute balances forward from postings (or server-side). §8.3.
5. **Multi-currency (PLN/USD/EUR).** Home = PLN; convert via `Price`/NBP rates for stats. FX
   transfers use `@@` — display natively, don't assume single currency (brief §6).
6. **Sign convention.** Income negative, expenses positive. UI presents magnitudes correctly
   (Zerro already thinks in income/outcome; map signs at the provider). Reimbursement income + its
   expense net ~zero (brief §6) — Stats should not double-count.
7. **Diacritics.** Any client-side name matching/slugify must transliterate Polish characters
   (brief §6). Prefer doing slugify server-side to match the pipeline exactly.
8. **Generated entries.** Not editable (fava.core guard) — the app must show them read-only.

---

## 10. Environment & runbook (already prepared + verified)

Workspace: `~/beancount-frontend/` — `repos/` (zerro, beancount, fava), `analysis/` (the 3 docs),
`spec/` (this + the brief), `ledger/sample.beancount` (a smoke-test ledger), `.venv/`.

Verified working (2026-07-03):
- **Node** v24.18.0 via nvm at `~/.nvm/versions/node/v24.18.0/bin` (add to PATH). **pnpm** 10.33.2
  via corepack.
- **Zerro**: `cd repos/zerro && pnpm install` done; `pnpm run lint` (tsc) clean; `pnpm run build`
  succeeds; dev server: `pnpm dev` (port 3000). Native builds (`esbuild`,`@parcel/watcher`)
  already approved via `pnpm-workspace.yaml`.
- **Python** 3.14.4 venv at `~/beancount-frontend/.venv` with `beancount==3.2.3`, `fava==1.30.14`,
  `beanquery==0.2.0` (installed from prebuilt wheels; gcc/make available if a rebuild is needed).
- **Smoke tests that pass**:
  - `.venv/bin/python -c "from beancount import loader; ..."` loads `ledger/sample.beancount`,
    realizes the account tree, 0 errors.
  - `.venv/bin/bean-query ledger/sample.beancount "SELECT account, sum(position) ..."` works.
  - `.venv/bin/fava -p 5112 ledger/sample.beancount` serves; JSON API confirmed at
    `/<slug>/api/ledger_data` and `/<slug>/api/query` (note the `<slug>` = filename-derived, e.g.
    `sample-ledger`).

Recommended first backend step: scaffold `zerro-bean-service` (FastAPI) importing `fava.core`,
point it at the real dev-copy ledger (below) or `ledger/sample.beancount`, implement `GET
/api/ledger` + `GET /api/transactions` + `POST /api/categorize`, and validate the durable-edit
round-trip locally before touching the production ledger on `192.168.1.115`.

**Real ledger — already imported as the local dev copy.** Dennis's actual Plinsburg Tech ledger is
extracted at `~/beancount-frontend/ledger-plinsburg/plinsburg-ledger/` (full pipeline + seed + all
`.beancount` files). It **loads clean: 779 entries, 551 transactions, 0 errors, currency PLN**.
`main.beancount` includes `prices/history/ongoing/manual`. This is the intended **dev/preview data
source** for building the Zerro UI. View it now with stock Fava:
```
cd ~/beancount-frontend/ledger-plinsburg/plinsburg-ledger
~/beancount-frontend/.venv/bin/fava -p 5112 -H 0.0.0.0 main.beancount   # → http://127.0.0.1:5112/
```
This is a **local copy**, fully separate from production (brief §7: `root@192.168.1.115`, dir
`/opt/fava/beancount/`, a git repo). Develop and preview here; deploy to the real host only in
Phase 6, copy-first, with the git history as the safety net.

---

## 11. Open decisions / questions for Dennis (with recommended defaults)

- **O1 — A1 confirmation (§1.3).** Confirm the ledger is authoritative going forward and the
  Firefly regen won't be re-run. *Default: yes.* If no, swap `POST /api/categorize` to write
  `overrides.json` + trigger `regen.sh` (brief §5) — frontend unaffected.
- **O2 — Stable id.** Add `id:` metadata to transactions (recommended) vs key everything by
  payee/entity + entry_hash. *Default: add `id:` (ask the pipeline author for the one-time
  emit).* (§8.5)
- **O3 — Analytics source.** Client-side compute from the transaction feed (recommended, reuses
  Zerro's Stats/Review code) vs server-computed balances/reports. *Default: client-side, with
  `GET /api/balances` as fallback.* (§6)
- **O4 — Merchant rules representation.** Sidecar `merchant_rules.json` (recommended, simple) vs
  beancount `custom` directives in the ledger. *Default: sidecar JSON.* (§7)
- **O5 — Deployment.** Run `zerro-bean-service` as a second container beside Fava on
  `192.168.1.115` behind the existing Caddy basic-auth (recommended) vs elsewhere. *Default:
  beside Fava.* (§2)
- **O6 — Repo strategy.** Fork Zerro in place (keep git history) vs new repo. *Default: fork in
  place on a branch; it's already cloned at `repos/zerro`.*

---

## 12. Suggested phased execution

1. **Phase 0 — Backend skeleton.** FastAPI + `fava.core`, `GET /api/ledger` + `GET
   /api/transactions` (normalized §5.3) against `ledger/sample.beancount`. Prove read.
2. **Phase 1 — Durable categorize.** `POST /api/categorize` (scope `txn` first, then `entity`
   bulk) with sha256 guard + reload + validation. Prove the edit round-trip and that it survives.
3. **Phase 2 — Frontend gut & rewire.** Delete §8.1; build the provider + slim store (§8.2); get
   the Transactions page reading real data and categorizing via the backend.
4. **Phase 3 — Categorize-by-merchant view + filters** (§4) — the headline UX, incl. the
   VRS-Reimbursed exclude filter and "uncategorized" quick filter. Split display.
5. **Phase 4 — Analytics.** Rewire Stats + Review (§6); multi-currency via prices.
6. **Phase 5 — Import loop.** beangulp importer + merchant rules (§7) for ongoing bank statements.
7. **Phase 6 — Deploy** beside Fava on the real ledger (copy-first, git-backed).

Each phase should end by driving the actual app/endpoint (not just tests) to confirm behavior on a
copy of the real ledger.
