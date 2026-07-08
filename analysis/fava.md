# Fava Architecture Analysis — Bridging a Browser UI to the Beancount Engine

*Analysis target: `/home/dennis/beancount-frontend/repos/fava` — the established Beancount web frontend (Python/Flask backend + Svelte/TS frontend). Goal: understand the browser↔Python bridge so we can reuse it behind a custom React frontend (adapting the Zerro budgeting app to Beancount).*

---

## 0. TL;DR

Fava is a **Flask app that wraps a single rich abstraction, `FavaLedger`, and exposes it over a flat JSON API** (`/<bfile>/api/<endpoint>`). The API is the whole contract: GET endpoints return report data (trees, journals, balances, commodities, options, query results), PUT/DELETE endpoints mutate the *text* of the Beancount file(s). Writes are done by **surgically splicing lines into the source `.beancount` files**, guarded by sha256 checksums to detect concurrent edits, then reloading the ledger. Budgeting is a **read-only reporting feature** driven by `custom "budget"` directives — it computes per-account, per-period target amounts, but has no notion of envelopes, categories, "money available to assign", or rollover. It is **not** expressive enough to back a Zerro/YNAB envelope UI as-is.

**Recommendation (see §6):** Reuse `FavaLedger` + `fava.core.file` as a library for the read + write-to-ledger plumbing, but do **not** rely on Fava's `custom "budget"` mechanism for envelope budgeting — implement the budget/envelope state as your own layer (either your own `custom` directive schema or a sidecar store). Running the whole Fava Flask app as a black-box backend is possible but awkward; a thin custom FastAPI/Flask service that imports `fava.core` is the sweet spot.

---

## 1. Backend architecture

### 1.1 Layout

```
src/fava/
  application.py      # Flask app factory, routing, multi-ledger loading, filters
  json_api.py         # the JSON API blueprint (the frontend contract)  <-- most important
  internal_api.py     # LedgerData + ChartApi (report-independent data + charts)
  serialisation.py    # (de)serialise entries <-> JSON
  context.py / _ctx_globals_class.py  # Flask `g` with per-request ledger + filters
  beans/              # thin wrapper over beancount.core (create, str, load, funcs, abc)
  core/
    __init__.py       # FavaLedger + FilteredLedger  <-- the core abstraction
    file.py           # reading/writing the ledger text files (insert/edit/delete)
    budgets.py        # custom "budget" parsing + computation
    accounts.py, attributes.py, charts.py, commodities.py, tree.py,
    query_shell.py, query.py, filters.py, ingest.py, watcher.py, ...
```

The Flask factory is `create_app(files, ...)` in `application.py`. It registers the JSON blueprint under a per-file URL prefix:

```python
fava_app.register_blueprint(json_api, url_prefix="/<bfile>/api")
```

So every API call is namespaced by a *ledger slug* (`<bfile>`), because Fava can serve several Beancount files at once (`_LedgerSlugLoader`). Most HTML routes just render an empty `_layout.html` shell and let the frontend fetch data via JSON (`CLIENT_SIDE_REPORTS` in `application.py`); the app is essentially an SPA host + JSON API.

A per-request `g` object (Flask context globals, `_ctx_globals_class.Context`) exposes `g.ledger` (the `FavaLedger`), `g.filtered` (a `FilteredLedger` built from the `account`/`filter`/`time` query params), `g.conv` (conversion), and `g.interval`. `_inject_filters` in `application.py` automatically threads the `conversion/interval/account/filter/time` query params through URLs, and `_perform_global_filters` calls `ledger.changed()` (reload-if-changed) before non-API requests.

### 1.2 `FavaLedger` — the core abstraction (`core/__init__.py`)

`FavaLedger` is *the* object a frontend backend needs. Constructed with a path to the main `.beancount` file, it loads everything and holds a set of sub-modules:

```python
class FavaLedger:
    all_entries: Sequence[Directive]          # all (unfiltered) entries
    load_errors: Sequence[BeancountError]
    options: BeancountOptions                 # the beancount options map
    fava_options: FavaOptions                 # Fava's own options
    prices: FavaPriceMap
    all_entries_by_type: EntriesByType        # entries grouped by directive type

    accounts: AccountDict                     # per-account details
    attributes: AttributesModule              # payees, narrations, tags, links, ...
    budgets: BudgetModule                     # custom "budget" handling
    charts: ChartModule
    commodities: CommoditiesModule
    file: FileModule                          # read/write the ledger text files
    ingest: IngestModule                      # importers
    query_shell: QueryShell                   # BQL queries
    ...
```

Key methods:

- `load_file()` — loads the main file + all includes via `beans.load.load_uncached`, groups entries by type, builds the price map, parses Fava options and each sub-module's state, and updates the file watcher. Called on construction and after every detected change / write.
- `get_filtered(account, filter, time)` — returns a `FilteredLedger` (memoised with `lru_cache(16)`). `FilteredLedger` applies `AccountFilter`, `AdvancedFilter`, and `TimeFilter` to `all_entries` and exposes `entries`, `root_tree`, `root_tree_closed`, `interval_ranges`, `prices`, `paginate_journal`, etc.
- `get_entry(entry_hash)` — find a directive by its content hash (`hash_entry`), used by all the per-entry endpoints. Raises `EntryNotFoundForHashError`.
- `context(entry_hash)` — returns the entry plus account balances *before* and *after* it.
- `account_journal(...)`, `interval_balances(...)` — per-account journals and interval balance trees (the latter also feeds budget columns).
- `commodity_pairs()`, `statement_path(...)`, `root_accounts`.
- `changed()` — see below.

### 1.3 Loading & watching / reload on change

- Loading goes through `fava.beans.load.load_uncached(path)`, i.e. it calls Beancount's loader and returns `(entries, errors, options)`.
- A **file watcher** is created in the constructor: `WatchfilesWatcher()` (Rust `watchfiles`, event-based) by default, or a polling `Watcher()` if `poll_watcher=True`. `paths_to_watch()` returns all `include`d files plus document directories and the importer module.
- `changed()` is the reload hook:

```python
def changed(self) -> bool:
    if self._is_encrypted:
        return False
    changed = self.watcher.check()
    if changed:
        self.load_file()
    return changed
```

Almost every GET endpoint calls `g.ledger.changed()` first, so the frontend always sees fresh data. The JSON success envelope also returns `mtime` (`json_success` → `{"data": ..., "mtime": str(g.ledger.mtime)}`), and the frontend polls `GET /api/changed` and compares mtimes (`stores/mtime.ts`) to know when to reload.

---

## 2. The JSON API — the contract

### 2.1 Framework / conventions (`json_api.py`)

All endpoints live in one Flask `Blueprint("json_api")` mounted at `/<bfile>/api`. A decorator does the routing based on the **function name**:

```python
def api_endpoint(func):
    method, _, name = func.__name__.partition("_")   # get_/put_/delete_
    # route = /<name>, methods=[method]
    # GET/DELETE args come from request.args (query string)
    # PUT args come from the JSON body
```

So `def get_source_slice(entry_hash: str)` becomes `GET /<bfile>/api/source_slice?entry_hash=...`, and `def put_source(...)` becomes `PUT /<bfile>/api/source` with a JSON body. Arguments are validated (string or list) by `validate_func_arguments`.

Every response is wrapped:

```python
def json_success(data):
    return jsonify({"data": data, "mtime": str(g.ledger.mtime)})
def json_err(msg, status):
    return jsonify({"error": msg})  # with HTTP status set
```

Errors map to HTTP statuses via `errorhandler`s (`ValidationError`→400, `EntryNotFoundForHashError`→404, `TargetPathAlreadyExists`→409, `FilterError`→400, etc.). Dataclasses (e.g. `SourceSlice`, `TreeReport`) are serialised to JSON by a custom `FavaJSONProvider`.

**Filter params** accepted by report endpoints (from query string, applied in `FilteredLedger`): `account`, `filter` (a Fava advanced-filter expression), `time` (date range like `2024`, `2024-01 - 2024-03`), plus `conversion` and `interval` (`year|quarter|month|week|day`) for tree/chart reports. The frontend groups these as `filters = [account, filter, time]` and `filters_conversion_interval` (adds `conversion`, `interval`) — see `frontend/src/api/index.ts`.

### 2.2 GET endpoints (read)

| Endpoint | Params | Returns (shape) |
|---|---|---|
| `changed` | – | `bool` — did the file change (and reload)? |
| `errors` | – | `SerialisedError[]` = `{type, source:{filename,lineno}\|null, message}` |
| `ledger_data` | – | `LedgerData` (big report-independent blob, see below) |
| `payee_accounts` | `payee` | `string[]` — accounts ranked for a payee (autocomplete) |
| `payee_transaction` | `payee` | last `Transaction` for that payee (serialised) or null |
| `narration_transaction` | `narration` | last `Transaction` for that narration |
| `narrations` | – | `string[]` |
| `query` | `query_string` + filters | BQL result: `QueryResultTable` (`{types, rows}`) or `QueryResultText` |
| `extract` | `filename`, `importer` | `Entry[]` — entries extracted by an importer |
| `context` | `entry_hash` | `{entry, balances_before, balances_after}` |
| `source` | `filename?` | `{file_path, sha256sum, source}` — raw file text |
| `source_slice` | `entry_hash` | `{slice, sha256sum}` — the source lines for one entry |
| `journal` | filters | `Directive[]` (all filtered entries, serialised) |
| `journal_page` | `page`, `order` + filters | `{page, total_pages, journal}` (journal is **rendered HTML**) |
| `events` | filters | `Event[]` |
| `documents` | filters | `Document[]` |
| `imports` | – | `FileImporters[]` — importable files |
| `options` | – | `{fava_options, beancount_options}` (both string maps) |
| `commodities` | filters | `[{base, quote, prices:[[date, number],...]}]` |
| `income_statement` | filters+interval | `TreeReport` = `{date_range, charts[], trees[]}` |
| `balance_sheet` | filters+interval | `TreeReport` (assets/liabilities/equity trees) |
| `trial_balance` | filters+interval | `TreeReport` (single root tree) |
| `account_report` | `a` (account), `r` (`changes`/`balances`/journal) + filters | `AccountReportJournal` **or** `AccountReportTree` (see §4) |
| `statistics` | filters | `{all_balance_directives, balances, entries_by_type}` |

**`LedgerData`** (`internal_api.py`) — fetched once on load, is the frontend's global state seed:

```python
accounts, account_details, base_url, currencies, currency_names,
errors, fava_options, incognito, have_excel, links, options,
payees, precisions, tags, years, user_queries, upcoming_events_count,
extensions, sidebar_links, other_ledgers
```

**Tree node shape** (`SerialisedTreeNode`, `core/tree.py`) — the backbone of all balance reports:

```python
{ account, balance, balance_children, children[], has_txns,
  cost?, cost_children? }
```
where `balance`/`balance_children` are `SimpleCounterInventory` maps of `{currency: number}`.

**Serialised entry shape** (`serialisation.py`) — every entry gets `t` (type name) and `entry_hash`. A `Transaction` serialises as `{t:"Transaction", entry_hash, date, flag, payee, narration, tags, links, meta, postings:[{account, amount, meta?}, ...]}`. `Balance`/`Price` carry `amount:{number, currency}`.

### 2.3 PUT / DELETE endpoints (mutations)

| Endpoint | Method | Body / params | Effect |
|---|---|---|---|
| `add_entries` | PUT | `{entries: Entry[]}` | deserialise + `file.insert_entries` — **the main "add transaction" path** |
| `source` | PUT | `{file_path, source, sha256sum}` | overwrite a whole source file (returns new sha256) |
| `source_slice` | PUT | `{entry_hash, source, sha256sum}` | replace the text lines of one entry |
| `source_slice` | DELETE | `entry_hash, sha256sum` | delete one entry's lines |
| `format_source` | PUT | `{source}` | align currencies (formatter), returns formatted text |
| `move` | PUT | `{account, new_name, filename}` | move a document file |
| `add_document` | PUT | multipart (`file`, `folder`, `account`, `hash?`) | upload a document, optionally attach to entry |
| `attach_document` | PUT | `{filename, entry_hash}` | add `document:` metadata to an entry |
| `upload_import_file` | PUT | multipart (`file`) | upload a file into the import dir |
| `document` | DELETE | `filename` | delete a document/import file |

Note: **there is no dedicated "edit transaction" endpoint** — editing means either re-writing an entry's source slice (`put_source_slice`) or editing the whole file (`put_source`). "Delete transaction" = `delete_source_slice`. Adding is `add_entries`. This is the entire write surface — small and file-centric.

Non-API Flask routes worth knowing (`application.py`): `GET /<bfile>/document/?filename=` (download a document), `GET /<bfile>/statement/?entry_hash=&key=`, `GET /<bfile>/download-query/...`, `GET /<bfile>/download-journal/`, and `/<bfile>/extension/...` for plugins.

---

## 3. Editing / writing back to the ledger (`core/file.py`)

Fava never keeps a database — the `.beancount` **text files are the source of truth**, and writes are line-level text edits, all serialised through a `threading.Lock` on `FileModule` and followed by `ledger.load_file()`.

### 3.1 Adding an entry — `insert_entries` → `insert_entry`

`FileModule.insert_entries` sorts entries and, for each, calls the module-level `insert_entry(entry, default_filename, insert_options, currency_column, indent)`:

```python
def insert_entry(entry, default_filename, insert_options, currency_column, indent):
    filename, lineno = find_insert_position(entry, insert_options, default_filename)
    content = to_string(entry, currency_column, indent)   # serialise directive -> text
    path = Path(filename)
    contents = path.read_text().splitlines(keepends=True)  # (readlines)
    if lineno is None:
        contents += "\n" + content          # append to end of file
    else:
        contents.insert(lineno, content + "\n")
    path.write_text(...)                     # preserving the file's newline style
    # then shift any insert-options below the insertion point
```

- `find_insert_position` supports Fava's `2017-01-01 custom "fava-option" "insert-entry" "Expenses:...."` markers: an entry is inserted just above the first matching `insert-entry` regex option whose date is before the entry's date; otherwise it is **appended to the default file** (`fava_options.default_file` or the main file).
- Serialisation to text is `fava.beans.str.to_string(entry, currency_column, indent)` — this is how a directive object becomes properly-aligned Beancount source. Entries come in as JSON via `deserialise` (`serialisation.py`), which for a Transaction parses each posting's `amount` string through Beancount's own parser (`parse_string`) so amounts/costs/prices are validated.

### 3.2 Editing / deleting an entry — source slicing

An entry knows its `filename` + `lineno` (`get_position`). `find_entry_lines(lines, lineno)` grabs the entry's block (the starting line plus all following indented/continuation lines until a blank line or a non-indented line). `get_entry_slice(entry)` returns `(entry_source, sha256(entry_source))`.

- `save_entry_slice(entry, source_slice, sha256sum)`: re-reads the current lines, recomputes the sha256, and **raises `ExternallyChangedError` if it doesn't match** the caller's `sha256sum` — this is the concurrency guard. Then it splices the new text in place and returns the new sha256.
- `delete_entry_slice` is the same with an emptied slice (also eating trailing blank lines).
- Whole-file writes (`set_source`) do the same sha256 check against the *entire file*.

### 3.3 Safeguards summary

1. **sha256 optimistic-concurrency** on both whole-file and per-entry writes → `ExternallyChangedError` (HTTP 409-ish) if the file/entry changed underneath you.
2. **`threading.Lock`** around every mutation.
3. **`GeneratedEntryError`** — entries whose filename starts with `<...>` or have no line number (plugin-generated, padding, etc.) can't be edited (`_get_position`).
4. **Reload after write** (`load_file`) + watcher `notify`, so in-memory state stays consistent.
5. Newline style of the original file is detected and preserved.
6. Extension hooks (`after_write_source`, `after_insert_entry`, `after_entry_modified`, `after_delete_entry`) fire on every mutation.

**For a React frontend this is the key reusable machinery**: it gives you validated "add transaction", "edit transaction text", and "delete transaction" against real Beancount files, with safe concurrency, for free.

---

## 4. Budgeting in Fava (`core/budgets.py`)

### 4.1 Mechanism

Budgets are parsed from `custom "budget"` directives:

```
2015-04-09 custom "budget" Expenses:Books "monthly" 20.00 EUR
```

`parse_budgets` reads each such Custom entry into:

```python
class Budget(NamedTuple):
    account: str          # values[0]
    date_start: date      # the directive's date
    period: Interval      # values[1]: year|quarter|month|week|day (INTERVALS map)
    number: Decimal       # values[2].number
    currency: str         # values[2].currency
```

They are stored as `{account: [Budget, ...]}`. A budget entry is a **recurring target that stays in effect from its `date_start` until superseded** by a later budget entry for the same account+currency.

### 4.2 Computation

`calculate(account, date_from, date_to)` computes a per-currency amount for an arbitrary date range by summing a **daily-prorated** value:

```python
for day in days_in_daterange(date_from, date_to):
    for budget in matching_budgets_on(day):        # last-seen per currency
        currency_dict[budget.currency] += budget.number / budget.period.number_of_days(day)
```

i.e. a `20 EUR monthly` budget contributes `20/daysInThatMonth` per day, so any window (a week, a quarter, a partial month) yields a correctly-prorated target. `calculate_children` sums this over all sub-accounts of a prefix.

### 4.3 How it surfaces

- In the **account report** (`get_account_report` with `r=changes|balances`): `AccountReportTree.budgets` is `{account: [{budget, budget_children}, ...]}` aligned to the interval `dates`, so the UI shows a budget column next to each interval's actual balance.
- In **bar charts** (`charts.py` `DateAndBalanceWithBudget`): each interval bar carries a `budgets` map so a target line can be drawn.

### 4.4 Is this enough for an envelope / zero-based (Zerro/YNAB) UI? — **No.**

Fava's budgets are a **reporting overlay on expense accounts**: "I intend to spend N per period on account X." What they *do* give you: recurring per-account, per-period, per-currency targets with proration and time-based supersession, computed for any window. That maps cleanly to YNAB's "monthly budgeted amount per category" if categories ≙ expense accounts.

What they **lack** for envelope budgeting:

- **No "to be budgeted" / money-available pool.** Envelope budgeting is fundamentally about *assigning already-existing money* to envelopes and enforcing that assigned ≤ available. Fava budgets are independent targets with no conservation constraint.
- **No rollover / carryover of leftover envelope balances** month to month (the core of YNAB). Fava's number resets each period.
- **No explicit per-month assignment** — you can't say "in July I moved 50 from Groceries to Dining"; you can only set a recurring target that changes on a date.
- **No envelope balance = budgeted − spent + carryover** state; Fava only computes target vs. actual for display.
- **No categories separate from the account tree**, no goals, no scheduled/target-by-date.
- It's **read-only** — there is no API to *set* a budget; you'd have to write `custom "budget"` directives yourself via `add_entries`/`source` edits.

**Conclusion:** Use Fava/Beancount for the ledger truth (accounts, transactions, actual spending), but model the **envelope state (monthly assignments, carryover, "to be assigned")** yourself. Two viable representations: (a) your own `custom` directive schema (e.g. `custom "zerro-assign" "2026-07" Expenses:Groceries 300.00 EUR`) parsed by your own module — keeps everything in the plain-text ledger and diffable; or (b) a sidecar store (SQLite/JSON) keyed by month+category, with actuals pulled from Beancount. Fava's `custom "budget"` can still be *generated* from your envelope state if you want Fava-compatibility, but it cannot be the primary store.

---

## 5. Frontend (brief)

- **Stack:** Svelte 5 + TypeScript, built with esbuild (`frontend/build.ts`), no runtime framework CDN. Source in `frontend/src/`. Charts use d3; the editor uses CodeMirror (with a Beancount language mode and a BQL mode under `frontend/src/codemirror/`).
- **API client:** `frontend/src/api/index.ts` is a clean, fully-typed wrapper over every JSON endpoint. Each endpoint is declared with `define_endpoint(name, validator, accepted_params, method)`; `api_url` builds `${base_url}api/${endpoint}?...`; `fetch_and_handle_api_call` unwraps `{data, mtime}`, updates the mtime store, and runs a **runtime validator** on `data`.
- **Validation:** `frontend/src/lib/validation.ts` is a small combinator library (`object`, `array`, `string`, `number`, `record`, `tuple`, `optional`, `constants`); `frontend/src/api/validators.ts` and `frontend/src/entries/` define validators/types mirroring the Python dataclasses (`LedgerData`, tree reports, entries, errors). These TS types are effectively a **machine-checked copy of the API contract**.
- **Entries:** `frontend/src/entries/` has classes for `Transaction`, `Posting`, `Balance`, etc., with `.validator` and helpers to build/serialise entries for `add_entries` — directly reusable as a reference for a React client.
- **Query helpers:** BQL editor/table support under `frontend/src/codemirror/` and `frontend/src/reports/query/`.

**Reusable for React (as reference, not import):** the endpoint list + param names in `api/index.ts`, the response validators/types in `api/validators.ts` + `entries/`, and the entry-serialisation shape. You would re-implement the fetch layer in your React stack but can copy the contract almost verbatim.

---

## 6. Reuse recommendation

**Three options, in increasing amount of custom code:**

**A. Run stock Fava as a black-box backend behind React.**
Feasible — the JSON API is complete and stable, and Fava can run headless (`create_app`, or `fava` CLI). You'd point React at `/<bfile>/api/*`. Downsides: everything is namespaced by `<bfile>` slug; some endpoints return **rendered HTML** (`journal_page`, `account_report` journal variant) rather than data; responses are wrapped in `{data, mtime}`; CORS/auth are your problem; and, critically, there is **no budgeting write API and no envelope model**. You'd bolt your envelope layer on the side and use `add_entries`/`source` for writes. Good for a fast prototype.

**B. (Recommended) Thin custom Python service that imports `fava.core`.**
Instantiate `FavaLedger(path)` yourself inside a small FastAPI/Flask service and expose exactly the endpoints your React app needs. You get, for free and battle-tested:
- loading + include handling + **file watching/auto-reload** (`FavaLedger.load_file` / `changed`),
- the account tree / balances / filtered views (`FilteredLedger`, `Tree`, `interval_balances`),
- **safe writes** — `file.insert_entries` (add), `file.save_entry_slice` (edit), `file.delete_entry_slice` (delete), all with sha256 concurrency guards,
- entry (de)serialisation (`fava.serialisation`) and text rendering (`fava.beans.str.to_string`),
- BQL queries (`query_shell`), commodities/prices, importers.

On top of that you add **your own budget/envelope module** (assignments, carryover, "to be assigned"), stored either as custom directives you parse yourself or a sidecar DB, with actual spending derived from `FavaLedger`. This avoids both the awkwardness of Fava's HTML-ish endpoints and the impedance mismatch of its `custom "budget"` model, while keeping the hard, correctness-critical parts (parsing, writing, reloading) in mature code. It also lets you shape responses (no `<bfile>` slug, plain JSON, your auth) for a React/Zerro data model.

**C. Write everything directly on `beancount` (skip Fava).**
Only worth it if you want zero Fava dependency. You'd re-derive the source-slicing/sha256 write logic, the reload-on-change watcher, and entry serialisation — all of which Fava has already solved well. Not recommended; you'd be re-implementing `fava.core.file` and `fava.beans`.

**Bottom line:** Go with **B** — treat `FavaLedger` + `fava.core.file` + `fava.serialisation`/`fava.beans` as your Beancount access/write library, expose a small purpose-built JSON API modeled on (but simpler than) Fava's, and own the envelope-budgeting layer entirely yourself because Fava's `custom "budget"` mechanism is a reporting target, not a zero-based/envelope system.

---

### Key file references

- API contract: `src/fava/json_api.py`, wrappers in `frontend/src/api/index.ts`, types in `frontend/src/api/validators.ts`
- Core ledger: `src/fava/core/__init__.py` (`FavaLedger`, `FilteredLedger`)
- Writes/safeguards: `src/fava/core/file.py` (`insert_entry`, `save_entry_slice`, `delete_entry_slice`, sha256 checks)
- Entry <-> JSON: `src/fava/serialisation.py`; directive -> text: `src/fava/beans/str.py` (`to_string`), builders in `src/fava/beans/create.py`
- Budgets: `src/fava/core/budgets.py` (`parse_budgets`, `calculate`, `calculate_budget`)
- App wiring / routes / reload: `src/fava/application.py`, watcher in `src/fava/core/watcher.py`
- Report-independent data & charts: `src/fava/internal_api.py`; trees in `src/fava/core/tree.py`
