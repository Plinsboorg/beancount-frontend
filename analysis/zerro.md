# Zerro Architecture Analysis — for a Beancount rework

Zerro is a React + TypeScript + Redux Toolkit single-page app implementing
zero-based (envelope) budgeting on top of the **Zenmoney** personal-finance API.
It is organized with Feature-Sliced Design (FSD): layers `1-app`, `2-pages`,
`3-widgets`, `4-features`, `5-entities`, `6-shared`, plus non-FSD `store`,
`worker`, `demoData`. Higher layers may import lower ones only.

All file paths below are absolute-relative to
`/home/dennis/beancount-frontend/repos/zerro/`.

---

## 0. Executive map of the data pipeline

```
Zenmoney /v8/diff  ──fetch──▶  TZmDiff  ──convertDiff.toClient──▶  TDiff
   (web worker, src/worker/worker.ts + src/6-shared/api/zenmoney/fetchDiff.ts)
        │                                    (src/6-shared/api/zm-adapter)
        ▼
   IndexedDB (idb)  ◀── saveLocalData ──  TLocalData
   (src/6-shared/api/storage.ts)
        │
        ▼  applyServerPatch / applyClientPatch
   Redux slice `data`  ──▶  TDataStore  (normalized ById<> maps of raw entities)
   (src/store/data/slice.ts, applyDiff.ts)
        │
        ▼  memoized selectors (reselect / createSelector)
   Derived entities:  account, tag, merchant, debtor, envelope
        │
        ▼
   Budget engine (src/5-entities/envBalances/*):
       rawActivity ▶ activity ▶ envMetrics ▶ monthTotals
        │
        ▼
   React UI (2-pages / 3-widgets / 4-features)
```

Two crucial architectural facts up front:

1. **The Redux store holds the *raw Zenmoney model*, essentially verbatim.**
   `TDataStore` is a normalized map of Zenmoney entities. The client types
   (`TAccount`, `TTransaction`, …) equal the Zenmoney types (`TZmAccount`,
   `TZmTransaction`, …) with only timestamp-unit changes. Everything above the
   store (envelopes, balances, budget engine) is computed from this raw model.

2. **Zerro's own budgeting data has no home in Zenmoney, so it is smuggled
   inside Zenmoney *reminders* as JSON blobs.** Envelope budgets, goals,
   envelope metadata (structure/order/currency/carry), user settings, and FX
   rates are all serialized into the `comment` field of dummy reminders attached
   to a hidden `🤖 [Zerro Data]` account (see §4.3, the "hidden store"). This is
   the single most Zenmoney-specific design decision in the app and the biggest
   thing a Beancount backend would replace.

---

## 1. Data model (`src/5-entities/*`, types in `src/6-shared/types/`)

The canonical type definitions live in
`src/6-shared/types/data-entities.ts` and `src/6-shared/types/types.ts`.
Pattern throughout: `TZmX` = raw server shape; `TX = TZmX & { changed: TMsTime }`
(server sends unix seconds, client stores ms). So the client model **is** the
Zenmoney model.

### 1.1 Primitive/shared types (`types.ts`)

```ts
export type TISOMonth = `${TYear}-${TMonth}`      // "2024-01"
export type TISODate  = `${TYear}-${TMonth}-${TDate}` // "2024-01-01"
export type TUnixTime = number   // seconds (server)
export type TMsTime   = number   // ms (client)
export type TUnits    = number   // money as a plain float in the entity's currency
export type TFxCode   = string   // currency short code, e.g. "USD","RUB" (from instrument.shortTitle)
export type TFxAmount = Record<TFxCode, number>   // multi-currency bag
export type TRates    = Record<TFxCode, number>
```

Money is a **float in a single currency per leg**; multi-currency sums are
carried as `TFxAmount` bags (`{ USD: 10, RUB: 500 }`) and only collapsed to one
number via an FX converter when needed. There is no integer-minor-units or
decimal representation — this differs sharply from Beancount's `Decimal` amounts.

### 1.2 Instrument / currency (`data-entities.ts`, `5-entities/currency`)

```ts
export type TZmInstrument = {
  id: TInstrumentId          // number
  changed: TUnixTime
  title: string              // "US Dollar"
  shortTitle: TFxCode        // "USD"  ← the currency code used everywhere as TFxCode
  symbol: string             // "$"
  rate: number               // rate vs the user's home instrument
}
```

Currencies are **Zenmoney "instruments" (numeric ids)**. `instrument.shortTitle`
(e.g. `"USD"`) is the `TFxCode` string used throughout the budget engine.
`5-entities/currency/instrument` exposes `getInstruments`, `getInstCodeMap`
(id→code). `5-entities/currency/fxRate` provides `TFxConverter`
(`(amount: TFxAmount, target: TFxCode, month) => number`) used to compare/sum
multi-currency amounts, with rates themselves stored in the hidden store.

### 1.3 Account (`5-entities/account`)

```ts
export enum AccountType { Cash, Ccard, Checking, Loan, Deposit, Emoney, Debt } // string values
export type TZmAccount = {
  id: TAccountId            // string (UUID)
  user: TUserId
  instrument: TInstrumentId // account's currency
  title: string
  type: AccountType
  balance: TUnits           // current balance (authoritative, from server)
  startBalance: TUnits
  creditLimit: TUnits
  inBalance: boolean        // counts toward net worth
  savings: boolean
  archive: boolean
  private: boolean
  syncID: string[] | null   // bank sync ids
  company: TCompanyId | null
  ... (loan/deposit terms: percent, capitalization, payoffStep, endDateOffset…)
}
```

Key derivations (`account/selectors.ts`, `account/shared/populate.ts`):
- `getPopulatedAccounts` adds `fxCode` (via instrument) and an `inBudget` flag.
- `getInBudgetAccounts` = accounts where `inBudget` is true → these define
  "money that is being budgeted". **This is the boundary of the envelope system.**
- `getSavingAccounts` = non-budget, non-debt accounts (each becomes an envelope).
- `getDebtAccountId` = the single account with `type === Debt` (Zenmoney models
  all counterparties/debts through one virtual debt account; see debtors).
- **Account balances are computed by walking transactions backward from the
  server-provided current `balance`** (`5-entities/accBalances/getBalances.ts`),
  not by summing postings forward. Beancount is the opposite (balance = sum of
  postings from the beginning), so this is a meaningful inversion.

### 1.4 Transaction (`5-entities/transaction`) — the core

Zenmoney transactions are **single objects with paired income and outcome legs**,
not double-entry postings:

```ts
export type TZmTransaction = {
  id: TTransactionId        // string
  date: TISODate
  created: TUnixTime
  changed: TUnixTime
  deleted: boolean
  hold: boolean | null
  viewed?: boolean

  // OUTCOME leg (money leaving an account)
  outcomeAccount: TAccountId
  outcomeInstrument: TInstrumentId
  outcome: TUnits
  outcomeBankID: TCompanyId | null

  // INCOME leg (money entering an account)
  incomeAccount: TAccountId
  incomeInstrument: TInstrumentId
  income: TUnits
  incomeBankID: TCompanyId | null

  tag: TTagId[] | null      // categories (0..n; only tag[0] is treated as the category)
  merchant: TMerchantId | null
  payee: string | null
  originalPayee: string | null
  comment: string | null
  mcc: number | null
  reminderMarker: TReminderMarkerId | null
  // "operation" original-currency amounts, geo, qrCode…
}
```

**Transaction type is *derived*, not stored** — this is the semantic heart of
the model (`5-entities/transaction/helpers.ts`):

```ts
export enum TrType { Income, Outcome, Transfer, IncomeDebt, OutcomeDebt }
export function getType(tr, debtId?) {
  if (debtId && tr.incomeAccount  === debtId) return TrType.OutcomeDebt
  if (debtId && tr.outcomeAccount === debtId) return TrType.IncomeDebt
  if (tr.income && tr.outcome)               return TrType.Transfer
  if (tr.outcome)                            return TrType.Outcome
  return TrType.Income
}
```

So:
- **Income**: only `income`/`incomeAccount` set.
- **Outcome**: only `outcome`/`outcomeAccount` set.
- **Transfer**: both legs set (moves between own accounts; may differ in currency → transfer fee).
- **Income/OutcomeDebt**: one leg is the special debt account → a loan to/from a debtor.

`isDeleted` also treats near-zero amounts as deleted; "permanent delete" sets
amounts to `0.00001` (a Zenmoney-ism, since you cannot truly delete via diff).
All writes go through `applyClientPatch` with `changed: Date.now()`
(`transaction/thunks.ts`).

**Mapping to Beancount:** one Zenmoney transaction ≈ one Beancount transaction
with two postings (outcome-account negative, income-account positive), the tag
becoming an expense/income account or `#tag`, merchant/payee → payee, comment →
narration. Multi-currency transfers map to postings with `@` price / cost.
Beancount transactions can have N postings; Zerro's 2-leg model is a strict
subset, so importing Beancount → Zerro's shape is lossy for split transactions
(Zerro "splits" transfers into two rows as a workaround — see `splitTransfer`).

### 1.5 Tag (category) (`5-entities/tag`)

```ts
export type TZmTag = {
  id: TTagId                // string (UUID) or the literal "null"
  title: string
  parent: TTagId | null     // one level of nesting
  icon: TIconName | null
  color: number | null
  showIncome / showOutcome / budgetIncome / budgetOutcome: boolean
  required: boolean | null
}
```

Tags are Zenmoney's category system (2-level: parent tag + child). The pseudo-tag
`"null"` = "no category". `5-entities/tag/model` populates tags with derived
color/symbol (`TTagPopulated`).

### 1.6 Merchant, Reminder, User, Debtors

- **Merchant** (`5-entities/merchant`): `{ id, user, title }`. Named payees.
- **Reminder / ReminderMarker** (`5-entities/reminder`): scheduled/planned
  transactions. **Heavily repurposed** as Zerro's private key-value store (§4.3).
- **User** (`5-entities/user`): `{ id, currency (home instrument), parent, country, … }`.
  `getRootUser`/`getRootUserId` = the user with no parent; `getUserCurrency` =
  home `TFxCode`. Nearly everything needs a `user` id to write entities.
- **Debtors** (`5-entities/debtors`): a *synthetic* entity, not from the server.
  Derived from transactions touching the debt account, keyed by merchant id or
  cleaned payee name, with running balances. Each debtor becomes an envelope.

### 1.7 Envelope — Zerro's own budgeting abstraction (`5-entities/envelope`)

The **envelope is Zerro's invention layered on top of Zenmoney**. An envelope is
a budgetable bucket derived from a tag, a savings account, or a debtor:

```ts
export enum EnvType { Tag, Account, Merchant, Payee }
export type TEnvelopeId =
  | `tag#${TTagId}` | `account#${TAccountId}`
  | `merchant#${TMerchantId}` | `payee#${string}`

export type TEnvelope = {
  id: TEnvelopeId
  type: EnvType
  entityId: string          // the underlying ZM entity id
  name / originalName / symbol / colorDisplay …
  children: TEnvelopeId[]   // one level of nesting
  parent: TEnvelopeId | null
  index: number             // display order
  visibility, group, comment
  currency: TFxCode         // budgeting currency (may differ from ZM entity)
  keepIncome: boolean       // income into this env stays here, not "to be budgeted"
  carryNegatives: boolean   // roll negative balance to next month
}
```

Envelopes are assembled by `getEnvelopes.ts::getCompiledEnvelopes`: build from
tags + saving accounts + debtors, then overlay `TEnvelopeMeta` (parent/order/
currency/carry/keepIncome — read from the hidden store, `shared/metaData.ts`),
then flatten into a tree (`shared/structure.ts`). `envId.get/parse`
(`shared/envelopeId.ts`) encode/decode `type#id`.

So envelope *identity* is Zenmoney-derived (`tag#<uuid>`), but envelope
*configuration* is Zerro-private.

### 1.8 Budget & Goal

Two parallel budget representations exist:

- **Zenmoney budget** (`TZmBudget`, `5-entities/budget/tagBudget`): the native
  Zenmoney monthly budget row, keyed `${date}#${tag}` with `income`/`outcome`
  amounts. Used only if the user opts into `preferZmBudgets`.
- **Zerro "env budget"** (`5-entities/budget/envBudget`): `Record<TEnvelopeId,
  number>` per month, stored in the **hidden store**. This is the default.

`5-entities/budget/getBudgets.ts` merges the two into
`ByMonth<Record<TEnvelopeId, number>>`, preferring env budgets. `setBudget.ts`
routes writes to either `setTagBudget` (native) or `setEnvBudget` (hidden).

**Goals** (`5-entities/goal`): `Record<TEnvelopeId, TGoal | null>` per month,
also in the hidden store.

```ts
export enum goalType { MONTHLY, MONTHLY_SPEND, TARGET_BALANCE, INCOME_PERCENT }
export type TGoal = { type: goalType; amount: number; end?: TISODate }
```

`shared/calcGoals.ts` computes goal progress (`needNow`, `needStart`,
`targetBudget`, `progress`) from a `TContext` of `{leftover, budgeted,
available, generalIncome, month}`. **This logic is pure and backend-agnostic.**

### 1.9 Relationship summary

```
instrument (currency) ─┬─ account.instrument
                       └─ transaction.income/outcomeInstrument
user ── owns ── account, tag, merchant, reminder, transaction, budget
transaction ── income/outcomeAccount ──▶ account
transaction ── tag[] ──▶ tag ;  merchant ──▶ merchant
tag / savingAccount / debtor ──derive──▶ envelope   (Zerro concept)
envelope ──has per month──▶ budget (number) + goal (TGoal)   (Zerro concept, hidden store)
```

---

## 2. Data source / API layer

### 2.1 The Zenmoney client (`src/6-shared/api/zenmoney/`)

- `endpoints.ts`: hardcoded `api.zenmoney.ru` / `api.zenmoney.app` OAuth + `/v8/diff/` URLs.
- `auth.ts`: OAuth2 authorization-code flow via popup window; posts
  `client_id/secret` to get a bearer token. Token kept in `tokenStorage`
  (localStorage, `6-shared/api/tokenStorage.ts`).
- `fetchDiff.ts`: the **only** network call for data. POSTs a `TZmRequest`
  (`= TZmDiff & { currentClientTimestamp }`) with `Authorization: Bearer`,
  returns a `TZmDiff`. Supports a `fakeToken` for demo/backup (no network).

The whole sync protocol is Zenmoney's **timestamp-based diff**: client sends
`serverTimestamp` of last sync + any locally changed entities; server returns
everything changed since, plus `deletion[]`. This is a bidirectional
last-write-wins sync, **not** a git-like or file-based model.

### 2.2 The adapter (`src/6-shared/api/zm-adapter/`)

`converters.ts::convertDiff` is the **entire translation layer** between server
and client. It is nearly an identity map — the only transforms are:
- unix seconds ⇄ ms on `changed`/`created`/`paidTill`/`stamp`;
- adding a synthetic `id = ${date}#${tag}` to budgets (`toBudgetId.ts`).

There is **no semantic remapping** — client entities are Zenmoney entities. This
adapter is where a Beancount backend would produce/consume `TDiff` objects, but
because it's so thin, the Zenmoney shape leaks straight into the store.

### 2.3 Persistence & worker (`src/worker/`, `src/6-shared/api/storage.ts`)

- `storage.ts`: IndexedDB via `idb`, one object store `serverData` in db
  `zerro_data`, key = entity domain (`transaction`, `account`, …), value = the
  full array for that domain. Simple `get/set/clear`.
- `worker/worker.ts`: a **Comlink-exposed web worker** that owns (a) network
  sync (`sync` = `convertDiff.toServer` → `fetchDiff` → `convertDiff.toClient`),
  (b) reading/writing IndexedDB (`getLocalData`/`saveLocalData`/`clearStorage`),
  and (c) `convertZmToLocal` for importing raw ZM backups. It exists to keep the
  (potentially large) diff conversion and idb I/O off the main thread. It holds
  **no app state** — it's a stateless I/O + transform helper.

### 2.4 Is there a clean provider boundary?

**Partly.** The *mechanism* is well isolated: all data ingress/egress funnels
through `worker.sync` / `worker.saveLocalData` / `worker.getLocalData`, and all
mutations go through two Redux actions, `applyServerPatch` (authoritative) and
`applyClientPatch` (local optimistic). The sync orchestration is one thunk,
`4-features/sync.ts::syncData`. Auth/bootstrap thunks are in
`4-features/authorization.ts` and `4-features/localData.ts`.

**But the *data shape* is not abstracted.** The provider speaks `TDiff` /
`TDataStore`, which *are* the Zenmoney model. There is no neutral "Ledger" or
"DataProvider" interface — the seam is a diff-of-Zenmoney-entities, not a
domain-neutral contract. See §5.

---

## 3. State management (`src/store/`)

- `store/index.ts`: RTK `configureStore` with reducers `data`, `isPending`,
  `lastSync`, `token`, `displayCurrency`. `immutableCheck`/`serializableCheck`
  disabled (large state, perf). Typed `useAppSelector`/`useAppDispatch`.
- `store/data/slice.ts`: the `data` slice, shape
  `{ current: TDataStore; server?: TDataStore; diff?: TDiff }`.
  - `current` = server data + local unsynced changes (what the UI reads).
  - `server` = last known server truth.
  - `diff` = accumulated local changes not yet pushed.
  - `applyServerPatch` writes into `server` then sets `current = server` and
    clears `diff` (with a TODO noting this is too aggressive).
  - `applyClientPatch` mutates `current` and merges into `diff` (`mergeDiffs.ts`).
  - `applyDiffMutable` (`shared/applyDiff.ts`) applies additions/updates by id
    and processes `deletion[]`.
- `store/data/selectors.ts`: base selectors (`getDiff`, `getLastSyncTime`,
  `getRootUserId`, `getDebtAccountId`).

**Data flow raw → derived:** everything above `state.data.current` is computed by
memoized `createSelector` chains living *inside the entities* (FSD keeps
selectors next to the model). E.g. `state.data.current.transaction`
→ `trModel.getTransactionsHistory` → … → budget engine. Each entity exposes a
`xModel` object (e.g. `accountModel`, `trModel`, `envelopeModel`, `budgetModel`,
`goalModel`, `fxRateModel`, `balances`) bundling selectors + hooks + thunks.
Writes are thunks that build a `TDiff` and dispatch `applyClientPatch`; the sync
thunk later pushes the accumulated `diff`.

The heavy computation lives in two entity clusters:
- **`5-entities/accBalances`** — account/debtor balances over time.
- **`5-entities/envBalances`** — the envelope budget engine (§4).

Both are pure `createSelector` pipelines wrapped in `withPerf` timers.

---

## 4. Budget engine (`src/5-entities/envBalances/`)

This is the heart of Zerro. It's a staged selector pipeline (files are literally
numbered; see `dataflow.dot`). `index.ts` exposes them as `balances.*`.

### 4.1 Stage pipeline

1. **`1 - monthList.ts`** — the list of months to compute (history start → a few
   months ahead).
2. **`1 - currentFunds.ts`** — total money currently in budget accounts (a `TFxAmount`).
3. **`1 - rawActivity.ts`** — the core aggregation. Walks every non-deleted
   transaction, keeps only those touching an in-budget account, classifies via
   `trModel.getType`, and buckets the money change per month into
   `internal` (transfer fees), `income[envId]`, `outcome[envId]`. The mapping
   transaction → envelope (`getEnvelope`) is: `Income/Outcome` → `tag#<tag[0]>`;
   `*Debt` → `merchant#…`/`payee#…`; `Transfer` → the counterpart `account#…`.
   Amounts are `TFxAmount` bags keyed by instrument short code; also produces a
   31-slot daily `trend`.
4. **`2 - activity.ts` / `2 - sortedActivity.ts`** — reshape raw activity into
   per-month `envActivity.byEnv[envId]`, `generalIncome`, `transferFees`, and a
   `total`, with filtering modes.
5. **`3 - envMetrics.ts`** — **the envelope math per month.** For each envelope
   (children first, then parents) computes:
   - `selfLeftover` = previous month's `selfAvailable`, gated by `carryNegatives`
     (`getLeftover`): positive always carries; negative carries only if the
     envelope opts in, else resets to 0.
   - `selfBudgeted` = the budget number for that env/month.
   - `selfActivity` = summed activity from stage 3.
   - `selfAvailable = leftover + budgeted + activity + childrenOverspend`
     (converted to the envelope's currency).
   - Parents also roll up `children*` (surplus vs overspend split) into
     `total*` metrics. This is the classic **available = carryover + budgeted −
     spent** envelope formula, with parent/child aggregation and per-envelope
     currency.
6. **`4 - monthTotals.ts`** — the month-level "to be budgeted" logic. Computes
   `fundsEnd/Start/Change`, `budgeted`, `available`, `overspend`,
   `budgetedInFuture`, `freeFunds = fundsEnd − available`, and:

   ```ts
   toBeBudgeted:
     freeFunds < 0                    → state 'negative', value = freeFunds
     freeFunds − budgetedInFuture > 0 → state 'positive', value = that surplus
     else                             → state 'allocated', value = {}
   ```

   i.e. **"To Be Budgeted" = money in budget accounts − money already allocated
   to envelopes (this month and future).** This walks months in reverse so
   future budgeting reduces what's available now.

### 4.2 Coupling of the engine to Zenmoney

The math (stages 3–4, goals `calcGoals.ts`) is **pure and data-source-agnostic**
— it operates on envelopes, budgets, activity, and FX amounts. The coupling is
concentrated in **stage 1–2 (`rawActivity`/`activity`)**, which read the raw
Zenmoney transaction shape directly:
- `tr.income/outcome/incomeAccount/outcomeAccount/incomeInstrument/…`,
- `trModel.getType` and the debt-account convention,
- `instruments[tr.incomeInstrument].shortTitle` to get currency codes,
- the "in-budget account" and debtor conventions.

If transactions were normalized to a neutral posting model at ingest, stages 3–4
would need essentially no change.

### 4.3 The hidden store — where Zerro's budgeting data actually lives

`src/5-entities/shared/hidden-store/`. Because Zenmoney has no place to store
envelope budgets/goals/metadata, Zerro persists them **inside Zenmoney reminders**:

- `dataAccount.ts` — lazily creates one account titled `🤖 [Zerro Data]` to
  anchor all hidden reminders (so they can be bulk-deleted).
- `monthlyStoreFactory.ts` / `simpleStoreFactory.ts` — create stores whose
  `getData` selector scans all reminders, JSON-parses each `comment`, and keeps
  those matching a `HiddenDataType`. `setData` writes a reminder with
  `comment = JSON.stringify({ type, month, payload })` via `setReminder`.
- `types.ts::HiddenDataType`: `Goals, FxRates, Budgets, LinkedAccounts,
  LinkedDebtors, EnvelopeMeta, UserSettings, TagOrder`.

Consumers: `budget/envBudget/budgetStore.ts`, `goal/goalStore.ts`,
`envelope/shared/metaData.ts`, `userSettings`, `currency/fxRate/fxRateStore.ts`.

**This entire mechanism is a Zenmoney workaround.** With a Beancount backend you
would store this config natively (a sidecar file, ledger metadata, or a small
JSON), and the hidden-store layer could be deleted rather than ported.

---

## 5. Swap assessment: coupling and the seam to replace

### 5.1 How coupled is it? — Verdict: **moderately, and cleanly localized.**

- The **UI layers** (`2-pages`, `3-widgets`, most `4-features`) talk only to
  `xModel` selectors/thunks and derived types (`TEnvelope`, `TEnvMetrics`,
  `TrType`, `TFxAmount`). ~68 UI files reference entity models; almost none
  reference `TZm*` or the Zenmoney API directly. A grep for
  `zenmoney|TZm|zm-adapter` across `src` hits only the api/adapter/worker/
  sync/auth files plus the type definitions and the Auth page. **The UI is
  effectively backend-agnostic already.**

- The **budget engine math** (envMetrics, monthTotals, calcGoals, envelope
  structure) is pure and portable.

- The **coupling is concentrated** in: (a) the entity type definitions being
  literally the Zenmoney shape; (b) the raw-activity/balance selectors reading
  that shape; (c) the diff-sync protocol; (d) the reminder-based hidden store;
  (e) OAuth. Everything Zenmoney-specific lives in a handful of directories.

### 5.2 The seam to replace

The natural seam is the **worker data-provider boundary + the `applyServerPatch`
diff contract**. Concretely, a Beancount backend must:

1. Read a Beancount ledger and produce a `TDiff` / `TDataStore` (accounts,
   transactions, currencies) — replacing `worker.sync` + `convertDiff.toClient`.
2. Consume local changes (`state.data.diff`) and write them back to the ledger
   (append/rewrite transactions, update balances) — replacing
   `convertDiff.toServer` + `fetchDiff`.
3. Store Zerro's budgeting config (budgets/goals/meta/settings) somewhere native
   — replacing the entire hidden-store + reminder machinery.

There are two viable strategies:

- **(A) Impersonate the Zenmoney model** — make the Beancount provider emit
  `TDataStore`-shaped data (fabricate instruments, accounts, single-object
  income/outcome/transfer transactions, a debt account, reminders for config).
  *Least code changed* — the store, entities, engine, and UI are untouched — but
  you inherit the awkward single-object transaction model, the float amounts,
  the balance-from-current-backward computation, and the reminder hack. Good for
  a fast first cut.

- **(B) Introduce a neutral domain model** — define provider-agnostic
  `Account/Posting/Transaction/Commodity` types, port stages 3–4 of the engine
  and the UI selectors onto them, and write a Beancount provider (and optionally
  keep a Zenmoney one). *More work*, but removes the Zenmoney warts and matches
  Beancount's double-entry/Decimal model. This is the "right" long-term shape.

### 5.3 File-level breakdown

**Rewrite / replace (Zenmoney-specific):**

- `src/6-shared/api/zenmoney/*` — auth, endpoints, fetchDiff (entire dir).
- `src/6-shared/api/zm-adapter/*` — converters, toBudgetId.
- `src/6-shared/api/zmPreferenceStorage.ts`, `tokenStorage.ts` (OAuth token).
- `src/worker/worker.ts` (the `sync` method and ZM conversion; idb helpers can stay).
- `src/4-features/authorization.ts`, `src/4-features/sync.ts`,
  `src/4-features/shared/getDataToSave.ts` — sync/auth orchestration.
- `src/2-pages/Auth/*` — the Zenmoney login UI.
- `src/5-entities/shared/hidden-store/*` and its consumers'
  storage layer (`budgetStore.ts`, `goalStore.ts`, `metaData.ts` write path,
  `fxRateStore.ts`, `userSettings` persistence) — the reminder hack.
- `src/6-shared/types/data-entities.ts` — the `TZm*`/`T*` entity shapes
  (redefine or map to a neutral model under strategy B).
- `src/5-entities/reminder/*` — only exists to serve the hidden store + planning;
  reassess.

**Adapt (couples to raw shape, but logic is reusable):**

- `src/store/data/*` — slice/applyDiff/mergeDiffs assume the ZM entity map; keep
  the two-action optimistic pattern, adjust the payload contract.
- `src/5-entities/transaction/{helpers,model,thunks,makeTransaction}.ts` —
  `getType`, income/outcome/transfer classification, write thunks. Rewrite if
  moving to real double-entry postings.
- `src/5-entities/account/*`, `debtors/*`, `currency/*` — populate/selectors read
  ZM fields; balance-from-current logic (`accBalances/*`) may invert for Beancount.
- `src/5-entities/envBalances/1 - rawActivity.ts` and `2 - activity.ts` — read
  raw transaction fields; the money-bucketing logic is reusable, the field access
  is not.
- `src/5-entities/budget/*` (tag vs env budget routing), `demoData/*`.

**Keep (data-source-agnostic — the value you preserve):**

- `src/5-entities/envBalances/{3 - envMetrics, 4 - monthTotals}.ts` — the
  carryover / available / to-be-budgeted engine.
- `src/5-entities/goal/shared/calcGoals.ts` (+ goal types) — goal math.
- `src/5-entities/envelope/*` — envelope abstraction, structure, ids (identity
  source changes from ZM ids to Beancount accounts, but the model stands).
- `src/6-shared/helpers/money/*` (FxAmount arithmetic), `helpers/date/*`.
- Essentially all of `2-pages`, `3-widgets`, and non-sync `4-features`
  (`moveMoney`, `budget`, `bulkActions`, `export`, `envelope`) — the entire UI.
- `src/6-shared/ui/*`, theme, localization, icons.

### 5.4 Notable impedance mismatches to plan for

- **Amounts**: Zerro uses `number` floats per single currency; Beancount uses
  arbitrary-precision `Decimal` and can have >2 postings and prices/costs. Expect
  a real numeric model change (or careful float handling) — strategy B territory.
- **Transaction shape**: 2-leg income/outcome object vs N-posting double entry.
  Transfers, splits, and multi-currency legs map imperfectly (`splitTransfer`
  already hacks around this).
- **Balances**: Zerro derives balances backward from a server-authoritative
  current balance; Beancount computes forward from postings (+ `balance`
  assertions). `accBalances` needs reworking.
- **Sync semantics**: Zenmoney is a live last-write-wins diff API; a Beancount
  ledger is a local file (append-only-ish, human-edited). No `serverTimestamp`
  diff protocol — you'll likely re-parse the file and diff locally.
- **Config storage**: replace the reminder hack with native storage.
- **Currencies**: Zenmoney numeric instruments vs Beancount commodity symbols;
  `TFxCode` (string codes) is already close to Beancount's model — a plus.

---

## 6. TL;DR for planning

The UI and the budgeting engine (envelopes, carryover, to-be-budgeted, goals)
are the reusable crown jewels and are already backend-agnostic. Zenmoney coupling
is real but localized to: the entity type shapes, the raw-activity/balance
selectors that read those shapes, the diff-sync + OAuth in
`6-shared/api/zenmoney` + `worker`, and the reminder-based hidden store for
budget config. The cleanest seam is the worker data-provider +
`applyServerPatch(TDiff)` contract. Fastest path (A): make a Beancount provider
impersonate the Zenmoney `TDataStore`. Cleanest path (B): introduce a neutral
double-entry domain model, port engine stages 3–4 and the UI selectors onto it,
and write a Beancount provider — recommended given Beancount's Decimal /
N-posting nature.
