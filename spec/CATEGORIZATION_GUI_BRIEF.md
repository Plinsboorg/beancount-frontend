# Briefing: Categorization GUI for the Plinsburg Tech beancount ledger

**Audience:** the agent building a GUI to let Dennis categorize transactions in-app.
**Author:** the accounting-pipeline agent (built the current beancount/Fava system).
**Date:** 2026-07-03.

---

## 0. TL;DR — read this first

- The ledger you see in Fava (`history.beancount`) is **generated**, not authored. It is a
  *projection* of a **frozen raw dataset** through a **rules pipeline** (`convert.py` +
  `mapping.py`). It is overwritten on every rebuild.
- Therefore **a category is not stored on a transaction — it is *computed*.** You cannot make
  categorization stick by editing `.beancount` files (Fava's editor, direct writes, etc.): the
  next rebuild wipes it. This is *the* reason a new GUI is needed; Fava has no click-to-categorize.
- **The correct place to persist a category decision is the overlay layer** (keyed by transaction
  id or by merchant), after which the ledger is **re-derived**. Your GUI should be a **frontend to
  that overlay layer**, not a beancount text editor.
- Net: think of your job as *"an editor for a small set of override tables + a rebuild trigger,"*
  with Fava remaining the read-only reporting view.

---

## 1. System architecture & data flow

```
   [ Firefly III ]  ← DECOMMISSIONED as a live dependency (one-time history source only)
        │  (one-time SQL export, 2026-07)
        ▼
   seed/postings.jsonl   ← FROZEN, read-only. 1094 posting legs / 547 transactions.
   seed/categories.jsonl ← FROZEN. Firefly's per-transaction category (partial coverage).
        │
        ▼
   convert.py  +  mapping.py     ← THE PIPELINE (deterministic, pure)
        │   - consolidates messy counterparties into canonical entities
        │   - assigns each txn an account  Prefix:Category:Entity
        │   - applies overrides / tags / income-splits (the overlay layer)
        ▼
   history.beancount   ← GENERATED. Do NOT hand-edit. 547 txns, 158 accounts.
        │
        │   main.beancount includes:  prices + history + ongoing + manual
        ▼
   Fava (Docker, :2502, Caddy basic-auth)   ← READ-ONLY reporting UI
```

Rebuild command (idempotent): `bash ~/firefly_import/beancount/regen.sh`
It runs `convert.py` → `scp history.beancount` to the Fava host → `bean-check` (validation) →
`git commit` snapshot → restart Fava. **~5-10 s.** No Firefly involvement.

The ledger host is a Docker CT: `root@192.168.1.115`, ledger dir `/opt/fava/beancount/`
(a git repo — every rebuild is snapshotted, so nothing is ever silently lost).

### The 4 ledger files (all included by `main.beancount`)
| file | origin | editable? |
|---|---|---|
| `prices.beancount` | `fetch_prices.sh` (NBP FX rates) | regenerated |
| `history.beancount` | **generated** by `convert.py` from the frozen seed | **NO — wiped on rebuild** |
| `ongoing.beancount` | future bank imports (importer not built yet) | append-only |
| `manual.beancount` | hand/Fava entries (adjustments, cash) — NOT regenerated | **YES, persists** |

---

## 2. The account model (this is the "category" system)

Accounts are `{Income|Expenses}:<Category>:<Entity>`, e.g.:

```
Expenses:Electronics-Components:Mouser
Expenses:Travel:Wizz-Air
Expenses:VRS-Reimbursed:Epaka
Income:Sales-Revenue:Electronics-Consulting:Smarty-Co   ← category can be MULTI-LEVEL
Income:Reimbursement:Smarty-Co
```

- **Category = the middle segment(s).** It is what Dennis wants to edit. Categories can be
  **multi-level** (a `:`-separated path, e.g. `Sales Revenue:Electronics Consulting`).
- **Entity = the leaf** = the consolidated counterparty (many raw bank names → one entity).
- **Beancount sign convention:** income accounts are credit-normal → **negative balances** (correct,
  not a bug). Fava's Income Statement presents them right-way-up.

Current state: **547 transactions, 158 accounts, 25 distinct categories, ~85 non-VRS expense
merchants.** Categories in use:
`Accounting, Bank-Fees, Cash-Withdrawal, Electronics-Components, Government-Fees, Health-Medical,
Internal-Transfers, Meals, Office-Rent, Other, Owner-Capital, Professional-Services, Reimbursement,
Retail-Supplies, Returns, Salaries, Sales-Revenue, Shipping-Courier, Social-Security-ZUS,
Software-Subscriptions, Taxes, Telecom-Internet, Transport, Travel, VRS-Reimbursed.`

**Important nuance for the GUI:** categorization almost always follows the **merchant (entity)**,
not the individual transaction. All 60 "Retail-Supplies" legs are 16 shops; all 26 "Software"
legs are 7 vendors. So the natural editing unit is **per-entity** (~85 rows) — but the system
also supports **per-transaction** overrides for the exceptions (a merchant that spans categories).
The GUI should probably support both, defaulting to per-merchant.

---

## 3. The overlay layer — how a category is actually decided

`mapping.py` holds ordered override tables. `convert.py` resolves each transaction leg through
them. **This is the layer your GUI writes to.** Current precedence:

**Entity resolution** (for a raw counterparty name):
1. `TXN_OVERRIDE[jid]["entity"]` — per-transaction pin
2. `ENTITY_RULES` — 121 ordered regexes on a normalized name → canonical entity
3. fallback: slug of the raw name

**Category resolution** (for a leg's `(side, entity)`):
1. `TXN_OVERRIDE[jid]["category"]` — **per-transaction** (highest)
2. `CAT_OVERRIDE[(side, entity)]` — per-merchant hard override
3. **majority vote** of Firefly's `categories.jsonl` meta for that `(side, entity)`
4. `CAT_FALLBACK[entity]` — used only when no meta exists
5. `"Other"` / `"Other Income"`

**Other overlays keyed by transaction id (`jid`):**
- `TXN_TAGS[jid]` → cross-cutting beancount tags (e.g. `#vrs-reimbursed`) appended to the header.
  Tags don't change the account; Fava filters/totals by them.
- `INCOME_SPLIT[jid]` → splits a client payment's income leg into services vs reimbursement.
- `TXN_OVERRIDE[jid]["payee"]` → nicer payee text.

Current table sizes: `ENTITY_RULES` 121 · `CAT_OVERRIDE` 19 · `CAT_FALLBACK` 69 ·
`TXN_OVERRIDE` 77 · `TXN_TAGS` 76 · `INCOME_SPLIT` 19.

**Key takeaway:** to set a transaction's category, the mechanism already exists —
`TXN_OVERRIDE[jid] = {"category": "<Category>"}` (per-transaction) or
`CAT_OVERRIDE[("Expenses", "<Entity>")] = "<Category>"` (per-merchant). Then rebuild.
Your GUI's core write operation is exactly this.

---

## 4. Transaction identity — the `jid` (and a gotcha)

- Every transaction has a stable integer id, `jid` (the old Firefly journal id). It is the key
  for **all** per-transaction overlays.
- It lives in `seed/postings.jsonl` (field `jid`).
- **GOTCHA:** it was just **removed from the generated `history.beancount`** (Dennis found the
  `firefly_id:` metadata line noisy). So the id is *not* visible in the `.beancount` output or in
  Fava anymore — but it is still the internal key. **Your GUI must obtain `jid` from the seed, not
  from the beancount file.** (If you prefer, `convert.py` can be trivially re-taught to emit a
  hidden `id:` metadata — but see §5: reading the seed is cleaner.)

### Seed schema (`seed/postings.jsonl`, one JSON object per posting leg)
```json
{"jid":879,"date":"2026-04-07","type":"Deposit","descr":"INVOICE ...",
 "acct_id":9,"acct":"SMARTY CO. ...","acct_type":"Revenue account",
 "amount":"-9317.00","cur":"USD","famount":null,"fcur":null}
```
- A transaction = all legs sharing a `jid`. `acct_type` ∈ {Asset account, Expense account,
  Revenue account, Initial balance account}. Expense/Revenue legs are the categorizable ones.
- `seed/categories.jsonl`: `{"jid":1332,"cat":"Bank Fees"}` — Firefly's original category (partial).
- 3 asset accounts only: `Assets:Bank:PKO:{Firmowy(PLN), USD, EUR}`. Multi-currency (PLN/USD/EUR),
  FX transfers use `@@` cost. Balance assertions at 2026-05-01 must keep tying to the cent.

---

## 5. Recommended GUI architecture (strong suggestion, not a mandate)

**Frame the GUI as an editor for the overlay layer, backed by the seed — NOT a beancount editor.**

```
  ┌─────────────────────────────────────────────────────────────┐
  │  Categorization GUI (new)                                    │
  │                                                              │
  │  reads:   seed/postings.jsonl (txns + jid + raw data)        │
  │           + current computed category per txn (see below)    │
  │  shows:   filterable/sortable table; assign category by      │
  │           click; per-merchant or per-txn; bulk-apply         │
  │  writes:  overrides.json   (jid/entity → category/…)         │
  │  triggers: regen.sh  (rebuild + validate + deploy)           │
  └─────────────────────────────────────────────────────────────┘
```

Concrete recommendations:

1. **Externalize the overlays into a data file** (e.g. `overrides.json`) that BOTH `mapping.py`
   loads and the GUI writes. Today the overrides are Python literals inside `mapping.py`; having
   the GUI edit Python source is brittle. A tiny change to `mapping.py` — load
   `overrides.json` and merge into `TXN_OVERRIDE`/`CAT_OVERRIDE`/`TXN_TAGS` — decouples data from
   code cleanly. (I can make that change; coordinate with me.) Suggested shape:
   ```json
   {
     "txn": { "1246": {"category": "Electronics-Components"} },
     "entity": { "Mawi": {"category": "Retail-Supplies"} },
     "tags": { "1275": ["vrs-reimbursed"] }
   }
   ```

2. **Get "current state" for the table** in one of two ways:
   - (preferred) add a `convert.py --json` export mode that emits, per transaction:
     `{jid, date, payee, legs:[{account, category, entity, amount, ccy}], tags}`. One function,
     reuses the existing resolution logic — no drift. **Ask me to add this; it's ~20 lines.**
   - or parse `history.beancount` and re-attach `jid` by joining on the seed (fragile; avoid).

3. **Persistence = write `overrides.json` → run `regen.sh` → Fava updates.** Validation
   (`bean-check`) and a git snapshot happen automatically inside `regen.sh`; surface its exit
   status in the UI. Never write `history.beancount` directly.

4. **Category input:** free-text with autocomplete against the existing 25 categories, but ALLOW
   new categories (just a string) and multi-level paths (`A:B`). `convert.py` slugifies each
   segment into a valid account component, so the GUI can accept human text ("Software &
   Subscriptions") and let the pipeline normalize it (`Software-Subscriptions`).

5. **Editing unit:** default to **per-merchant** (fewer rows, matches how categories actually
   work), with a "this transaction only" escape hatch that writes a `txn` override (which
   out-ranks the merchant rule — precedence already supports this).

6. **Filters the GUI needs:** by category, by tag (esp. exclude `#vrs-reimbursed`), by
   merchant, by date range, by amount, by currency, and an "uncategorized / Other" quick filter.
   (Dennis's immediate want: *filter out anything already in VRS-Reimbursed, categorize the rest.*)

---

## 6. Complexity & gotchas (things that will bite you)

- **Generated ledger:** never trust edits to `history.beancount`; they vanish. Everything routes
  through the overlay + rebuild. This is the #1 conceptual trap.
- **`jid` not in the output** anymore (see §4) — read it from the seed.
- **Multi-currency:** PLN / USD / EUR. Amounts are native; don't assume PLN. FX transfers exist.
- **Polish diacritics:** raw counterparty names contain `ą ć ę ł ń ó ś ź ż` and uppercase forms.
  `mapping.norm()` transliterates them before regex matching — WITHOUT that, `URZĄD`→`URZD` and
  rules silently miss. Any name-matching you add must transliterate too.
- **Sign convention:** income legs are negative. Expenses positive. A "reimbursement" income leg
  and its matching expense are meant to net ~zero (pass-through, not profit).
- **Overrides already in place** you must not clobber: 76 txns tagged `#vrs-reimbursed` and
  force-categorized to `Expenses:VRS-Reimbursed`; 19 SmartyCo payments income-split into
  `Sales-Revenue:Electronics-Consulting` + `Reimbursement`; several placeholder/return fixes.
  Precedence matters — per-txn beats per-merchant beats meta-vote beats fallback.
- **Merchant ≠ transaction:** one entity can legitimately need per-transaction category (rare).
  Support the exception without forcing per-transaction as the default.
- **Validation is non-negotiable:** a bad override can break `bean-check` (e.g. unbalanced split).
  Always run `regen.sh` and check it prints `OK: validated`; the git snapshot lets you revert.
- **Don't reintroduce Firefly.** It's intentionally decommissioned; the seed is the frozen truth.

---

## 7. File & command reference

| path | what |
|---|---|
| `~/firefly_import/beancount/seed/postings.jsonl` | frozen raw txns (has `jid`) — **read-only** |
| `~/firefly_import/beancount/seed/categories.jsonl` | frozen Firefly categories |
| `~/firefly_import/beancount/convert.py` | the builder (resolution logic lives here) |
| `~/firefly_import/beancount/mapping.py` | the overlay tables (what to externalize) |
| `~/firefly_import/beancount/regen.sh` | rebuild → validate → deploy → snapshot → restart |
| `/opt/fava/beancount/` (on `root@192.168.1.115`) | deployed ledger, git-versioned |
| Fava UI | `http://192.168.1.115:2502` (Caddy basic-auth, user `dennis`) |

Validate a candidate build without deploying:
`SD=~/firefly_import/beancount python3 ~/firefly_import/beancount/convert.py && \
 docker -H ssh://root@192.168.1.115 exec fava bean-check /data/main.beancount` (or just run `regen.sh`).

---

## 8. Coordination

The pipeline author (me) can, on request and quickly:
- add `convert.py --json` export (transaction list with computed categories + `jid`),
- externalize overlays into `overrides.json` that `mapping.py` merges,
- re-add a hidden stable `id:` to the output if you prefer that over reading the seed.

Pick your integration seam and tell me which of the above you want; I'll wire the pipeline side
so your GUI has a clean, stable contract. The golden rule stays: **edit overlays → rebuild →
Fava reflects. Never edit the generated ledger.**
