# Beancount (v3.2.x) Data Model & Programmatic Interfaces

A reference for building a web frontend (adapting the React budgeting app **Zerro**) on top of a Beancount ledger.

Analyzed source: `/home/dennis/beancount-frontend/repos/beancount` (version **3.2.3**).

> **Key v3 change up front:** the query engine (`bean-query` / BQL) has been **extracted into a separate package `beanquery`** and is no longer shipped inside `beancount`. The `beancount` package now only installs `bean-check`, `bean-doctor`, `bean-example`, `bean-format`. See §2.

---

## 1. The Data Model / Directives

Beancount parses a plain-text `.beancount` file into a **date-sorted list of immutable directives** (`NamedTuple`s). All directive definitions live in `beancount/core/data.py`.

### Common attributes

Every directive carries two leading fields:

- **`meta: Meta`** — `Meta = dict[str, Any]`. Always contains `"filename"` and `"lineno"` (the source location — critical for editing, see §3). May also hold user metadata key/values.
- **`date: datetime.date`** — Beancount tracks dates only, never times. Line number is the secondary sort key.

Type aliases (`data.py:29-32`):

```python
Account  = str
Currency = str
Flag     = str
Meta     = dict[str, Any]
```

### The core directives (exact shapes from `beancount/core/data.py`)

**Open** (`data.py:93`) — declares an account exists.
```python
class Open(NamedTuple):
    meta: Meta
    date: datetime.date
    account: Account
    currencies: list[Currency]          # allowed currencies, or None = unrestricted
    booking: Optional[Booking]          # lot-matching method enum, usually None
```

**Close** (`data.py:118`) — account no longer usable after this date.
```python
class Close(NamedTuple):
    meta: Meta
    date: datetime.date
    account: Account
```

**Commodity** (`data.py:133`) — optional; declares a currency/commodity, mainly to attach metadata.
```python
class Commodity(NamedTuple):
    meta: Meta
    date: datetime.date
    currency: Currency
```

**Pad** (`data.py:155`) — auto-inserts a transaction to make the *next* Balance assertion on `account` succeed, sourcing the difference from `source_account` (typically an Equity opening-balances account).
```python
class Pad(NamedTuple):
    meta: Meta
    date: datetime.date
    account: Account
    source_account: Account
```

**Balance** (`data.py:177`) — assertion: `account` holds exactly `amount` at the **start** of `date`.
```python
class Balance(NamedTuple):
    meta: Meta
    date: datetime.date
    account: Account
    amount: Amount
    tolerance: Optional[Decimal]
    diff_amount: Optional[Amount]       # None if check passed; else the discrepancy
```

**Posting** (`data.py:206`) — one leg of a transaction. **Not a top-level directive**; it lives inside `Transaction.postings`. Note it does *not* carry its own `date`, and `meta` is optional/last.
```python
class Posting(NamedTuple):
    account: Account
    units: Optional[Amount]                       # None => inferred from other legs
    cost: Optional[Union[Cost, CostSpec]]         # lot cost basis (for commodities)
    price: Optional[Amount]                        # conversion price (@ syntax)
    flag: Optional[Flag]                           # per-posting flag, usually None
    meta: Optional[Meta]
```

**Transaction** (`data.py:239`) — the central object.
```python
class Transaction(NamedTuple):
    meta: Meta
    date: datetime.date
    flag: Optional[Flag]                # e.g. '*' (cleared) or '!' (pending)
    payee: Optional[str]
    narration: Optional[str]            # never None in practice ('' if absent)
    tags: frozenset[str]                # without '#'; EMPTY_SET if none
    links: frozenset[str]               # without '^'; EMPTY_SET if none
    postings: list[Posting]
```
Text form:
```
2024-03-15 * "Whole Foods" "Groceries" #food ^receipt-42
  Expenses:Food:Groceries   45.20 USD
  Assets:Checking          -45.20 USD
```

**Note** (`data.py:287`) — dated free-text attached to an account (v3 adds tags/links).
```python
class Note(NamedTuple):
    meta: Meta; date: datetime.date
    account: Account
    comment: str
    tags: Optional[frozenset[str]]
    links: Optional[frozenset[str]]
```

**Event** (`data.py:313`) — a named string variable that changes over time (`type`, `description`), e.g. `"location"`.

**Query** (`data.py:350`) — a named, saved BQL query (`name`, `query_string`).

**Price** (`data.py:372`) — price of `currency` in terms of `amount.currency` on `date`; builds the price database.
```python
class Price(NamedTuple):
    meta: Meta; date: datetime.date
    currency: Currency
    amount: Amount
```

**Document** (`data.py:397`) — attaches a file (`filename`) to an `account` on a date (+ tags/links).

**Custom** (`data.py:427`) — the generic escape hatch: an arbitrary dated record for experimental/plugin features. **This is what Fava's budgeting uses (see §4).**
```python
class Custom(NamedTuple):
    meta: Meta
    date: datetime.date
    type: str                # arbitrary label, e.g. "budget"
    values: list[Any]        # list of typed tokens (strings, accounts, amounts, dates, numbers)
```

`ALL_DIRECTIVES` / `Directive` union list all twelve types (`data.py:452-481`). `Directives = list[Directive]`.

### Amounts, numbers, cost, position

**Number** (`beancount/core/number.py`) — always Python `Decimal`, never float. Use `D(str)` to build (`number.py:41`); constants `ZERO`, `ONE`, and the sentinel class `MISSING` for not-yet-interpolated values.

**Amount** (`beancount/core/amount.py:40`) — a `(number, currency)` pair:
```python
class Amount(NamedTuple("Amount", [("number", Optional[Decimal]), ("currency", str)])):
    ...
```
Free functions (not methods): `amount.add/sub/mul/div/abs`, `Amount.from_string("45.20 USD")`. Arithmetic across different currencies raises.

**Cost / CostSpec** (`beancount/core/position.py:28,45`) — cost basis of a held lot.
```python
class Cost(NamedTuple):        # fully resolved lot
    number: Decimal            # per-unit cost
    currency: str
    date: datetime.date
    label: Optional[str]

class CostSpec(NamedTuple):    # user input pre-booking; any field may be None
    number_per: Optional[Decimal]
    number_total: Optional[Decimal]
    currency: Optional[str]
    date: Optional[datetime.date]
    label: Optional[str]
    merge: Optional[bool]
```

**Position** (`position.py:178`) = `(units: Amount, cost: Optional[Cost])`. **Inventory** (`beancount/core/inventory.py:81`) is a `dict` of `(currency, cost) -> Position`, i.e. a multi-currency, multi-lot balance. Helpers: `inv.get_currency_units(ccy) -> Amount`, `inv.split()`, `inv.reduce(convert.get_units/get_value/...)`.

### Account structure & the double-entry rule

Account names are colon-separated strings under **five root types** (`beancount/core/account_types.py:37`):

```python
DEFAULT_ACCOUNT_TYPES = AccountTypes("Assets", "Liabilities", "Equity", "Income", "Expenses")
```

- Components match `[\p{Lu}][\p{L}\p{Nd}\-]*` (first char uppercase). e.g. `Assets:Bank:Checking`, `Expenses:Food:Groceries`.
- `account.py` provides `split()`, `join()`, `parent()`, `leaf()`, `has_component()`.
- Sign convention: **Assets & Expenses increase with positive (debit) amounts; Liabilities, Equity & Income increase with negative (credit) amounts.**

**The balancing rule:** for every Transaction, the sum of posting **weights** across all currencies must be zero. The weight is defined in `beancount/core/convert.py:get_weight()`:

| Posting | Weight used to balance |
|---|---|
| `5234.50 USD` | `5234.50 USD` (the units) |
| `3877.41 EUR @ 1.35 USD` | `5234.50 USD` (price applied) |
| `10 HOOL {523.45 USD}` | `5234.50 USD` (cost applied) |

Exactly one posting may omit `units` (`None`); Beancount **interpolates** it to whatever makes the transaction balance. This is why a frontend can write a two-legged transaction and let Beancount fill the second amount, though it is safer to write both explicitly.

---

## 2. Reading a Ledger Programmatically

### Loading — `beancount.loader`

```python
from beancount import loader
entries, errors, options_map = loader.load_file("/path/ledger.beancount")
# or from text:
entries, errors, options_map = loader.load_string(text)
```

`load_file` signature (`beancount/loader.py:89`) returns the canonical triple:
- **`entries`** — a **date-sorted `list[Directive]`** (already parsed, booked, interpolated, plugins applied, validated).
- **`errors`** — `list` of error objects (each has `.source` meta, `.message`, `.entry`). A non-empty list does **not** raise; you must inspect it.
- **`options_map`** — `dict` of ledger options (`operating_currency`, `title`, account-type names, plugins, etc.).

Loading runs the full pipeline: parse (C lexer/grammar) → run plugins → **booking** (lot matching) → **interpolation** (fill missing amounts) → validation (Balance/Open checks). `load_file` also transparently caches (pickle) and handles encrypted files. `filter_txns(entries)` (`data.py:751`) yields just the Transactions.

### Querying — BQL / `beanquery` (now a separate package)

In v3 the SQL-like query language moved out of `beancount` into the standalone **`beanquery`** package (`pip install beanquery`, CLI `bean-query`). Beancount core no longer contains a `query/` module. Usage:

```python
from beanquery import connect
conn = connect("beancount://path/ledger.beancount")
curs = conn.execute("SELECT account, sum(position) GROUP BY account")
rows = curs.fetchall()
```

BQL supports `SELECT ... FROM ... WHERE ... GROUP BY ... ORDER BY`, aggregates (`sum`, `count`), and functions over postings (`account`, `position`, `cost`, `date`, `year`, `payee`...). It is convenient for reports but the frontend can equally compute directly from `entries` (below), avoiding a second dependency.

### Realized balances / account tree — `beancount.core.realization`

To get an **account tree with balances**, use `realize()` (`beancount/core/realization.py:211`):

```python
from beancount.core import realization
real_root = realization.realize(entries)          # tree root (RealAccount)
real_acct = realization.get(real_root, "Assets:Checking")
balance   = real_acct.balance                     # an Inventory
```

`RealAccount` (`realization.py:45`) subclasses `dict` (children keyed by account component) with slots:
- `account` — full name
- `txn_postings` — list of `TxnPosting`/`Open`/`Balance`/... attached to this node
- `balance` — an `Inventory` (final balance of this node's own postings, not children)

Helpers: `realization.get_or_create()`, `iter_children(real_account, leaf_only=False)`, `compute_postings_balance()`, and `iterate_with_balance()` which yields `(entry, postings, change, running_balance)` for a running-balance journal. For a plain per-account total without the tree, `realization.postings_by_account()` / `compute_balance` also exist. To convert an `Inventory` to a single number per currency use `inv.get_currency_units(ccy)` or `inv.reduce(convert.get_units)`.

---

## 3. Writing / Modifying a Ledger

Beancount is **read-oriented**: the loaded `entries` are immutable `NamedTuple`s and there is **no built-in "save ledger" API**. Files are edited as plain text. Two building blocks exist:

### Serializing directives back to text — `beancount/parser/printer.py`

- **`printer.format_entry(entry, dcontext=None, render_weights=False, prefix=None, write_source=False) -> str`** (`printer.py:444`) — renders one directive to canonical Beancount text using the `EntryPrinter` multi-method class (`printer.py:94`, one method per directive type). `prefix` allows custom indentation.
- `printer.print_entry()` / `print_entries()` write to a file object.
- Number formatting is controlled by a `DisplayContext` (`beancount/core/display_context.py`) built during load, so amounts print with the ledger's observed precision.

So the round-trip is: mutate/`_replace()` a NamedTuple (or build a fresh one with `data.new_metadata`, `data.create_simple_posting`) → `format_entry` → splice text into the file.

### The safe edit pattern — as implemented by Fava

Beancount ships no editor, but **Fava** (`/home/dennis/beancount-frontend/repos/fava`) implements the production pattern. All logic is in `fava/src/fava/core/file.py`, exposed over a JSON API in `fava/src/fava/json_api.py`.

**Serialization:** Fava does *not* write its own printer — `fava/beans/str.py:to_string()` strips internal (`_`-prefixed) metadata and calls **`beancount.parser.printer.format_entry(entry, prefix=" "*indent)`**, then runs its own `align()` (`fava/core/misc.py`) to line up the currency column (default column 61) and trims trailing whitespace.

**Appending / inserting a new entry** (`file.py`):
- `FileModule.insert_entries(entries)` sorts entries, then per entry calls module-level `insert_entry()`.
- `find_insert_position(entry, insert_options, default_filename)` decides *where*: it honors `custom "fava-option" "insert-entry" <date> <account-regex>` directives (parsed to `InsertEntryOption(date, re, filename, lineno)`), matching the entry's accounts against the regexes to insert near related entries; otherwise it **appends to the end** of the default file (`lineno=None`).
- Core (paraphrased `file.py`):
```python
filename, lineno = find_insert_position(entry, insert_options, default_filename)
content = to_string(entry, currency_column, indent)
contents = path.read_text().splitlines(keepends=True)
if lineno is None:
    contents += "\n" + content            # append
else:
    contents.insert(lineno, content + "\n")
```
It then bumps the stored `lineno` of any later insert-option in the same file so subsequent inserts stay aligned.

**Editing an existing entry — the sha256 concurrency guard** (the safety-critical part):
- An entry is located by `(filename, lineno)` read from `entry.meta`. Entries whose filename starts with `<` or have no lineno are **generated** (from plugins/Pad) and are **not editable** (`GeneratedEntryError`).
- `get_entry_slice(entry)` reads the file, `find_entry_lines()` collects the entry's text (from `lineno` until a blank/dedented line), and returns `(source_text, sha256sum_of_that_text)`.
- `save_entry_slice(entry, new_source, sha256sum)`:
```python
entry_lines  = find_entry_lines(lines, lineno-1)
entry_source = "".join(entry_lines).rstrip("\n")
if _sha256_str(entry_source) != sha256sum:
    raise ExternallyChangedError(path)          # file changed under us -> refuse
lines = [*lines[:first], new_source + "\n", *lines[first+len(entry_lines):]]
file.writelines(lines)
return _sha256_str(new_source)                  # new hash back to client
```
The client must round-trip the hash of the slice it originally fetched; if the on-disk slice no longer matches, the write is rejected instead of clobbering a concurrent change. Whole-file writes (`set_source`) use the identical hash check. `delete_entry_slice` likewise.

**JSON API endpoints Fava's frontend calls** (`json_api.py`, mounted under `/<slug>/api/`):
- `PUT /add_entries` — `insert_entries` from deserialized JSON entries.
- `GET /source` / `PUT /source` — whole-file read/write (returns/expects `sha256sum`).
- `GET /source_slice` / `PUT /source_slice` / `DELETE /source_slice` — single-entry by `entry_hash` (+ `sha256sum` guard).
- `PUT /format_source` — align/format text (the "format" button).

Entry<->JSON conversion for the API is `fava/serialisation.py` (`serialise`/`deserialise`), separate from the text serializer.

**Recommendation for the Zerro frontend:** mirror this pattern — build `Transaction`/`Posting` NamedTuples (or the equivalent JSON), serialize with `format_entry`, append/splice with a sha256 optimistic-lock, and always **reload** (`load_file`) after writing to re-derive balances and surface `errors`. Never edit generated entries. Simplest of all: **run Fava (or a thin Fava-like service) and call its JSON API**, rather than reimplementing the file surgery.

---

## 4. Budgeting

**Beancount has no native envelope/zero-sum budgeting.** Double-entry accounting tracks where money *went*, not forward-looking category allocations. Several conventions bolt budgeting on top:

### (a) Fava's native "budget" — `custom "budget"` directives (spending targets)
Fava (not Beancount core) reads **Custom directives** of type `"budget"` and renders budget-vs-actual columns:
```
2024-01-01 custom "budget" Expenses:Food        "monthly"  400.00 EUR
2024-01-01 custom "budget" Expenses:Transport   "weekly"    30.00 EUR
```
Semantics: for the named account, allocate `amount` per period (`"daily"|"weekly"|"monthly"|"quarterly"|"yearly"`), starting at the directive's date and continuing until superseded by a later budget directive for the same account. Fava's `BudgetModule` (`fava/src/fava/core/budgets.py`) parses these and **normalizes everything to a daily rate**, so it can compute a prorated budget for any arbitrary date range; a negative amount expresses an income target. This is a **per-account periodic target** model (planned vs actual per expense account) — **not** envelope budgeting: there is no pool of "money to assign," and unspent budget is **not** a running balance you can reallocate or carry over. The extension **fava_budget_freedom** (github.com/Leon2xiaowu/fava_budget_freedom) adds account wildcards and rollover on top of this directive.

### (b) fava-envelope (the closest thing to YNAB envelopes)
**fava-envelope** (github.com/**polarmutex**/fava-envelope, formerly bryall; PyPI `fava-envelope`) is a Fava extension **and** CLI implementing zero-based / envelope budgeting. It ignores the `"budget"` directive and uses its own `custom "envelope"` directives:
```
2000-01-01 custom "fava-extension" "fava_envelope" "{}"
2020-01-01 custom "envelope" "start date"     "2020-01"
2020-01-01 custom "envelope" "months ahead"   "2"
2020-01-01 custom "envelope" "budget account" "Assets:Checking"
2020-01-01 custom "envelope" "mapping"        "Expenses:Food:*" "Expenses:Food"
2020-01-31 custom "envelope" "allocate"       "Expenses:Food"   100.00
```
`budget account` (regex-capable) defines the **pool of money to allocate**; `allocate` funds a category for a given month; `mapping` collapses expense subtrees into buckets. It builds a month-by-month matrix of **allocated / spent / available**, with unspent amounts **carrying forward** — the defining YNAB/Zerro rollover behavior. Single operating currency only. Its `allocate` + `budget account` model is the natural template for a Zerro-style frontend.

### (c) Pure double-entry "envelope" accounts / CLI tools
The "true" beancount way to get rollover envelopes without extensions: model each envelope as an **Asset or Equity sub-account** and move real money into it each period, then spend from it, so unspent cash literally accumulates:
```
2024-01-01 * "Budget: assign to food"
  Equity:Budget:Available     -400.00 EUR
  Equity:Budget:Food           400.00 EUR
```
Because these are real balancing transactions, envelope balances roll over automatically and are fully auditable — the most faithful mapping of Zerro's "assign every unit a job" model onto beancount, at the cost of many bookkeeping entries. Mailing-list threads discuss `Equity:Budget` variants for zero-sum "give every dollar a job." Separately, **beancount-budget** (git.sr.ht/~goorzhel/beancount-budget) is a CLI envelope budgeter whose budget lives in **CSV files (not directives)**; it computes balances from the ledger and supports per-account **quotas** (default 0 = no overspending), recurring goals, and future large-purchase goals.

### The gap to fill for a Zerro-style frontend
Zerro/YNAB semantics — **"to be budgeted" pool, per-category monthly assignment, category balance = assigned − spent + carryover** — have **no first-class representation** in beancount. Options, roughly in order of fidelity:
1. **Reuse fava-envelope's model** (or its Custom-directive convention) so budgets live in the ledger and rollover math matches YNAB.
2. **Emit real Equity:Budget transactions** (approach c) — fully double-entry, auditable, native rollover, but verbose.
3. **Store assignments as `custom "budget"`/metadata** and compute carryover in the frontend — simplest to write, but rollover logic lives outside beancount and Fava's native budget view won't show rollovers.

Whatever the representation, **actuals** (spending per category, income, cash on hand) come straight from the loaded `entries`/realization for free; only the **assignment + carryover layer** is net-new data the frontend must own and persist. Concretely it must supply: a store of monthly *allocations*, a rollover engine, a "to-be-budgeted" (unassigned money) computation, overspend handling, and reconciliation against real account balances. **fava-envelope is the closest existing mechanism** and the recommended template.

**Selected sources:** fava-envelope (github.com/polarmutex/fava-envelope, PyPI `fava-envelope`); beancount.io "Budgeting in Beancount" (custom "budget" directive docs); fava_budget_freedom; beancount-budget CLI (git.sr.ht/~goorzhel/beancount-budget); Fava issue #909 "Envelope budgeting"; mailing list "Budgeting with beancount?".

---

## 5. Runtime Constraints (Python engine, JS frontend)

Beancount is Python (with a C-extension lexer/parser); a browser cannot run it directly. Options to bridge to a JS/React frontend:

**(a) Python web service exposing JSON — the Fava model. Recommended.**
Run beancount server-side; expose load/query/write over HTTP+JSON. Fava already does exactly this (Flask app, JSON API in `json_api.py`, incl. the safe sha256-guarded writes). You can (i) run Fava and drive its existing API from Zerro, (ii) write Fava extensions, or (iii) stand up a thin FastAPI/Flask service that calls `loader.load_file` + `realization` + `printer.format_entry`. **Pros:** uses the real, correct engine (booking, interpolation, plugins); write path already solved. **Cons:** requires a running Python backend (not a pure static SPA); needs deployment. This is by far the lowest-risk path.

**(b) Pyodide / WASM in the browser.**
Beancount's parser is a C extension (`_parser`, via Meson build). Pyodide can run pure-Python and *some* C extensions compiled to WASM, but beancount's native parser is **not** a standard pyodide-shipped package and would need a custom Emscripten build of the C grammar/lexer — nontrivial and unmaintained-by-upstream. **Feasibility: low/experimental.** Bundle size (CPython + Decimal + beancount) is also heavy (tens of MB). Viable only for a fully offline, no-backend app if someone maintains the WASM build; not recommended as a first approach.

**(c) Reimplement the parser/model in TypeScript.**
Write a TS parser for the (fairly small) grammar plus the balancing/interpolation and realization logic. **Pros:** pure client-side, no Python at all, full control, works as a static PWA (fits Zerro's architecture). **Cons:** you must faithfully re-implement booking, cost/lot matching, interpolation, Balance/Pad semantics, plugins — easy to get subtly wrong; you lose compatibility with the plugin ecosystem. Reasonable **if** you target a constrained subset (simple two-legged transactions, single operating currency, no cost-basis investing) — which is exactly the subset an envelope-budgeting app needs. A pragmatic hybrid: TS for reading/rendering the common subset, and shell out to a Python service (a) for anything complex or for validation.

**Bottom line:** Start with **(a)** — reuse Fava's proven load + JSON + safe-write stack (or a thin equivalent). Consider a **(c)** TS reader for the simple transaction subset if a zero-backend static app is a hard requirement; treat **(b)** as impractical for now.
