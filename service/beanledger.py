"""zerro-bean-service: ledger access layer.

Wraps fava.core.FavaLedger over the Plinsburg beancount ledger and provides:
  - build_zm_diff(): the full dataset in the Zenmoney TDiff shape Zerro's store
    expects (instruments, user, accounts, tags-as-categories, merchants-as-entities,
    2-leg transactions; splits become one pseudo-transaction per category leg).
  - categorize(): durable rewrite of a category posting's account via fava's
    sha256-guarded save_entry_slice, with auto `open` insertion + reload + validation.
  - set_project(): per-leg `project:` posting metadata (the app's only label
    concept; beancount #tags are no longer surfaced to the UI).
  - split_txn(): replace a transaction's category legs with a new set of legs
    (amount + category + project each) — same total, so the entry still balances.
  - delete_txn(): durable delete of a whole transaction.

Conventions (SPEC §3):
  category account = {Income|Expenses}:<Category...>:<Entity>; category = middle
  segments, entity = leaf. Equity legs act as category (opening balances) unless the
  transaction has no Assets leg, in which case the Equity account is the funding
  wallet (owner cash expenses).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from decimal import Decimal
from pathlib import Path

from beancount.core.data import Transaction
from fava.core import FavaLedger
from fava.core.file import get_entry_slice, save_entry_slice, delete_entry_slice
from fava.beans.str import to_string

FUND_ROOTS = ("Assets:", "Liabilities:")
CAT_ROOTS = ("Income:", "Expenses:")

# same slug as convert.py (diacritic-safe)
PLMAP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")


def slug(name: str) -> str:
    s = name.translate(PLMAP)
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip()
    parts = [p for p in s.split() if p]
    out = "-".join(w[:1].upper() + w[1:] for w in parts)
    if not out:
        out = "Unknown"
    if not (out[0].isupper() or out[0].isdigit()):
        out = "X" + out
    return out


def slug_category(cat: str) -> str:
    """Slugify a human category path, segment by segment ('A & B:C d' -> 'A-B:C-d')."""
    segs = [slug(s) for s in cat.split(":") if s.strip()]
    return ":".join(segs)


def slug_tag(name: str) -> str:
    """Slugify a beancount tag name ('#Trip Berlin' / 'trip berlin' -> 'trip-berlin').
    Lowercase, PL-transliterated, non-alphanumerics collapsed to hyphens."""
    s = name.translate(PLMAP).lower().lstrip("#")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def classify_legs(txn: Transaction):
    """Return (fund_legs, cat_legs) with indices into txn.postings."""
    assets = [(i, p) for i, p in enumerate(txn.postings) if p.account.startswith(FUND_ROOTS)]
    ie = [(i, p) for i, p in enumerate(txn.postings) if p.account.startswith(CAT_ROOTS)]
    equity = [(i, p) for i, p in enumerate(txn.postings)
              if not p.account.startswith(FUND_ROOTS + CAT_ROOTS)]
    if ie and not assets:
        # owner-cash style: Equity leg is the funding wallet
        return assets + equity, ie
    return assets, ie + equity


def cat_entity(account: str):
    """Split a category account into (root, category_path, entity)."""
    parts = account.split(":")
    root = parts[0]
    if len(parts) >= 3:
        return root, ":".join(parts[1:-1]), parts[-1]
    if len(parts) == 2:
        return root, parts[1], None
    return root, "", None


def tag_id_for(account: str) -> str:
    root, cat, _entity = cat_entity(account)
    return f"{root}:{cat}" if cat else root


def leg_project(txn: Transaction, posting) -> str | None:
    """Project of a category leg: `project:` posting metadata, falling back to
    transaction metadata (covers hand-written entries labelling the whole txn).
    Slugified so ids are stable regardless of how the value was typed."""
    raw = (posting.meta or {}).get("project") or txn.meta.get("project")
    if not raw:
        return None
    return slug_tag(str(raw)) or None


class BeanLedger:
    def __init__(self, path: str):
        self.path = Path(path).resolve()
        self.ledger = FavaLedger(str(self.path))
        self.lock = threading.RLock()
        self.ledger_dir = self.path.parent

    # ------------------------------------------------------------------ utils

    def changed(self) -> bool:
        with self.lock:
            return self.ledger.changed()

    def errors(self):
        return [
            {
                "message": e.message,
                "source": dict(e.source) if e.source else None,
            }
            for e in self.ledger.load_errors
        ]

    def _txns(self):
        return self.ledger.all_entries_by_type.Transaction

    def _txn_by_id(self, tid: str) -> Transaction:
        for t in self._txns():
            if t.meta.get("id") == tid:
                return t
        raise KeyError(f"transaction id not found: {tid}")

    def _rates(self):
        """latest CUR->PLN rate per currency"""
        rates = {}
        for p in self.ledger.all_entries_by_type.Price:
            if p.amount.currency == "PLN":
                rates[p.currency] = float(p.amount.number)  # keyed by date order
        return rates

    def git(self, *args, check=False):
        return subprocess.run(
            ["git", "-C", str(self.ledger_dir),
             "-c", "user.email=service@local", "-c", "user.name=zerro-bean", *args],
            capture_output=True, text=True, check=check,
        )

    def git_snapshot(self, msg: str):
        self.git("add", "-A")
        self.git("commit", "-m", msg, "--author", "zerro-bean <service@local>")

    # ------------------------------------------------------ zm-diff building

    def build_zm_diff(self) -> dict:
        with self.lock:
            self.ledger.changed()
            now = int(time.time() * 1000)
            txns = self._txns()
            rates = self._rates()

            currencies = ["PLN", "USD", "EUR"]
            for t in txns:
                for p in t.postings:
                    if p.units and p.units.currency not in currencies:
                        currencies.append(p.units.currency)
            inst_id = {c: i + 1 for i, c in enumerate(currencies)}
            symbols = {"PLN": "zł", "USD": "$", "EUR": "€"}
            instruments = [
                {
                    "id": inst_id[c],
                    "changed": now,
                    "title": c,
                    "shortTitle": c,
                    "symbol": symbols.get(c, c),
                    "rate": 1.0 if c == "PLN" else rates.get(c, 1.0),
                }
                for c in currencies
            ]

            user = {
                "id": 1, "changed": now, "currency": inst_id["PLN"], "parent": None,
                "country": 1, "countryCode": "PL", "email": "dennis@vrs.racing",
                "login": "dennis", "monthStartDay": 1, "isForecastEnabled": False,
                "planBalanceMode": "balance", "planSettings": "",
                "paidTill": now + 10 * 365 * 86400_000,
                "subscription": "10yearssubscription",
                "subscriptionRenewalDate": None,
            }
            country = {"id": 1, "title": "Poland", "currency": inst_id["PLN"], "domain": "pl"}

            # ---- collect funding accounts / categories / entities from txns + opens
            fund_curs: dict[str, list] = {}   # account -> [currencies]
            tag_ids: dict[str, dict] = {}
            entities: set[str] = set()
            projects: set[str] = set()

            def note_cat_account(account: str):
                root, cat, entity = cat_entity(account)
                tid = tag_id_for(account)
                if tid not in tag_ids:
                    tag_ids[tid] = {
                        "id": tid, "changed": now, "user": 1,
                        "title": cat or root, "parent": None,
                        "icon": None, "staticId": None, "picture": None, "color": None,
                        "showIncome": root == "Income", "showOutcome": root == "Expenses",
                        "budgetIncome": False, "budgetOutcome": False, "required": False,
                    }
                if entity:
                    entities.add(entity)

            for open_e in self.ledger.all_entries_by_type.Open:
                acc = open_e.account
                if acc.startswith(CAT_ROOTS):
                    note_cat_account(acc)

            for t in txns:
                fund, cats = classify_legs(t)
                for _i, p in fund:
                    fund_curs.setdefault(p.account, [])
                    if p.units.currency not in fund_curs[p.account]:
                        fund_curs[p.account].append(p.units.currency)
                for _i, p in cats:
                    note_cat_account(p.account)
                    proj = leg_project(t, p)
                    if proj:
                        projects.add(proj)

            for proj in sorted(projects):
                tag_ids[f"#{proj}"] = {
                    "id": f"#{proj}", "changed": now, "user": 1,
                    "title": f"#{proj}", "parent": None,
                    "icon": None, "staticId": None, "picture": None, "color": None,
                    "showIncome": False, "showOutcome": False,
                    "budgetIncome": False, "budgetOutcome": False, "required": False,
                }

            # ---- account ids: per-currency split when a fund account is multi-currency
            def acct_id(account: str, cur: str) -> str:
                curs = fund_curs.get(account, [cur])
                return account if len(curs) == 1 else f"{account}|{cur}"

            balances: dict[str, float] = {}
            for t in txns:
                fund, _cats = classify_legs(t)
                for _i, p in fund:
                    aid = acct_id(p.account, p.units.currency)
                    balances[aid] = balances.get(aid, 0.0) + float(p.units.number)

            def make_account(aid: str, account: str, cur: str) -> dict:
                leaf = account.split(":")[-1]
                title = leaf if len(fund_curs.get(account, [])) <= 1 else f"{leaf} {cur}"
                if account.startswith("Equity:"):
                    title = f"{leaf} {cur}" if len(fund_curs[account]) > 1 else leaf
                return {
                    "id": aid, "changed": now, "user": 1,
                    "instrument": inst_id[cur], "title": title,
                    "role": None, "company": None,
                    "type": "cash" if account.startswith("Equity:") else "checking",
                    "syncID": None,
                    "balance": round(balances.get(aid, 0.0), 2),
                    "startBalance": 0, "creditLimit": 0,
                    "inBalance": account.startswith(FUND_ROOTS),
                    "savings": False, "enableCorrection": False, "enableSMS": False,
                    "archive": False, "private": False,
                    "capitalization": None, "percent": None, "startDate": None,
                    "endDateOffset": None, "endDateOffsetInterval": None,
                    "payoffStep": None, "payoffInterval": None,
                }

            accounts = []
            for account, curs in sorted(fund_curs.items()):
                for cur in curs:
                    aid = acct_id(account, cur)
                    accounts.append(make_account(aid, account, cur))
            # Zerro expects a debt account to exist
            accounts.append({
                **make_account("DEBT", "Assets:DEBT", "PLN"),
                "id": "DEBT", "title": "Debts", "type": "debt", "instrument": inst_id["PLN"],
                "balance": 0, "inBalance": False,
            })

            merchants = [
                {"id": e, "changed": now, "user": 1, "title": e.replace("-", " ")}
                for e in sorted(entities)
            ]

            # ---- transactions
            out_txns = []
            for t in txns:
                tid = t.meta.get("id") or f"L{t.meta.get('lineno', 0)}@{Path(str(t.meta.get('filename', ''))).name}"
                date = t.date.isoformat()
                ms = int(time.mktime(t.date.timetuple()) * 1000)
                fund, cats = classify_legs(t)

                base = {
                    "changed": ms, "created": ms, "user": 1, "deleted": False,
                    "hold": False, "viewed": True, "qrCode": None,
                    "incomeBankID": None, "outcomeBankID": None,
                    "merchant": None, "payee": t.payee or None,
                    "originalPayee": t.payee or None,
                    "comment": t.narration or None,
                    "date": date, "mcc": None, "reminderMarker": None,
                    "opIncome": None, "opIncomeInstrument": None,
                    "opOutcome": None, "opOutcomeInstrument": None,
                    "latitude": None, "longitude": None,
                }

                if not fund:
                    continue  # nothing to anchor to (shouldn't happen)

                if not cats and len(fund) >= 2:
                    # transfer between own accounts
                    neg = next((p for _i, p in fund if p.units.number < 0), fund[0][1])
                    pos = next((p for _i, p in fund if p.units.number >= 0), fund[-1][1])
                    out_txns.append({
                        **base, "id": tid,
                        "tag": None,
                        "outcome": float(-neg.units.number),
                        "outcomeAccount": acct_id(neg.account, neg.units.currency),
                        "outcomeInstrument": inst_id[neg.units.currency],
                        "income": float(pos.units.number),
                        "incomeAccount": acct_id(pos.account, pos.units.currency),
                        "incomeInstrument": inst_id[pos.units.currency],
                    })
                    continue

                anchor = fund[0][1]
                aid = acct_id(anchor.account, anchor.units.currency)
                ainst = inst_id[anchor.units.currency]
                split = len(cats) > 1
                for k, (_i, p) in enumerate(cats):
                    n = float(p.units.number)
                    ptid = f"{tid}~{k}" if split else tid
                    root, cat, entity = cat_entity(p.account)
                    proj = leg_project(t, p)
                    tags = [tag_id_for(p.account)] + ([f"#{proj}"] if proj else [])
                    row = {
                        **base, "id": ptid, "tag": tags,
                        "merchant": entity if entity in entities else None,
                    }
                    if n > 0:  # money left the fund account (expense-like)
                        row.update({
                            "outcome": n, "outcomeAccount": aid, "outcomeInstrument": ainst,
                            "income": 0, "incomeAccount": aid, "incomeInstrument": ainst,
                        })
                    else:      # money entered the fund account (income-like)
                        row.update({
                            "income": -n, "incomeAccount": aid, "incomeInstrument": ainst,
                            "outcome": 0, "outcomeAccount": aid, "outcomeInstrument": ainst,
                        })
                    out_txns.append(row)

            return {
                "serverTimestamp": now,
                "instrument": instruments,
                "country": [country],
                "company": [],
                "user": [user],
                "merchant": merchants,
                "account": accounts,
                "tag": list(tag_ids.values()),
                "budget": [],
                "reminder": [],
                "reminderMarker": [],
                "transaction": out_txns,
                "deletion": [],
            }

    # ------------------------------------------------------------- categorize

    def _ensure_opens(self, accounts: list[str]):
        """Append `open` directives to history.beancount for unknown accounts."""
        known = {o.account for o in self.ledger.all_entries_by_type.Open}
        needed = []
        for acc in accounts:
            if acc not in known:
                known.add(acc)
                needed.append(acc)
        if needed:
            hist = self.ledger_dir / "history.beancount"
            with hist.open("a", encoding="utf-8") as f:
                f.write("\n")
                for acc in needed:
                    f.write(f"2024-01-01 open {acc}\n")

    def _rewrite_entries(self, edits: list[tuple[Transaction, list[tuple[int, str]]]]):
        """Apply account rewrites. edits: [(txn, [(posting_index, new_account)])].
        Batched bottom-up per file so line numbers stay valid, single reload after.
        """
        self._ensure_opens([acc for _t, changes in edits for _idx, acc in changes])

        def position(t: Transaction):
            return (str(t.meta.get("filename")), int(t.meta.get("lineno", 0)))

        for txn, changes in sorted(edits, key=lambda e: position(e[0])[1], reverse=True):
            postings = list(txn.postings)
            for idx, acc in changes:
                postings[idx] = postings[idx]._replace(account=acc)
            new_entry = txn._replace(postings=postings)
            slice_str, sha = get_entry_slice(txn)
            save_entry_slice(txn, to_string(new_entry, 33, 2), sha)

        self.ledger.load_file()

    def categorize(self, scope: str, target: str, category: str,
                   apply_to_future: bool = False, side: str = "Expenses",
                   force: bool = False) -> dict:
        """scope 'txn': target = txn id (optionally 'id~legIdx' for splits).
        scope 'entity': target = entity leaf (e.g. 'Mouser'); rewrites the matching
        legs on `side` of every txn with that entity. category = human path,
        slugified here.

        Protection (SPEC §4.4): entity-scope edits skip vrs-reimbursed txns
        (legacy #tag or `project:` metadata) and legs already in a protected
        category, unless force=True. Per-txn edits are always explicit, so they
        are not blocked.
        """
        PROTECTED_TAGS = {"vrs-reimbursed"}
        PROTECTED_PROJECTS = {"vrs-reimbursed"}
        PROTECTED_CATS = {"VRS-Reimbursed"}
        cat = slug_category(category)
        if not cat:
            raise ValueError("empty category")

        with self.lock:
            self.ledger.changed()
            errors_before = len(self.ledger.load_errors)
            edits = []

            if scope == "txn":
                tid, _, leg = target.partition("~")
                txn = self._txn_by_id(tid)
                _fund, cats = classify_legs(txn)
                k = int(leg) if leg else 0
                if k >= len(cats):
                    raise ValueError(f"no category leg {k} on {tid}")
                idx, posting = cats[k]
                if not posting.account.startswith(CAT_ROOTS):
                    raise ValueError(f"leg {posting.account} is not categorizable")
                root, _old, entity = cat_entity(posting.account)
                new_acc = f"{root}:{cat}:{entity}" if entity else f"{root}:{cat}"
                if new_acc != posting.account:
                    edits.append((txn, [(idx, new_acc)]))

            elif scope == "entity":
                skipped = 0
                for txn in self._txns():
                    if not force and (txn.tags or set()) & PROTECTED_TAGS:
                        skipped += 1
                        continue
                    _fund, cats = classify_legs(txn)
                    changes = []
                    for idx, p in cats:
                        if not p.account.startswith(CAT_ROOTS):
                            continue
                        root, oldcat, entity = cat_entity(p.account)
                        if root != side or entity != target:
                            continue
                        if not force and oldcat.split(":")[0] in PROTECTED_CATS:
                            skipped += 1
                            continue
                        if not force and leg_project(txn, p) in PROTECTED_PROJECTS:
                            skipped += 1
                            continue
                        new_acc = f"{root}:{cat}:{entity}"
                        if new_acc != p.account:
                            changes.append((idx, new_acc))
                    if changes:
                        edits.append((txn, changes))
            else:
                raise ValueError(f"unknown scope {scope}")

            n = sum(len(c) for _t, c in edits)
            if edits:
                self._rewrite_entries(edits)
                self.git_snapshot(f"categorize {scope} {target} -> {cat} ({n} postings)")

            if apply_to_future and scope == "entity":
                rules_file = self.ledger_dir / "merchant_rules.json"
                rules = {}
                if rules_file.exists():
                    rules = json.loads(rules_file.read_text())
                rules[target] = cat
                rules_file.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")

            new_errors = self.errors()
            return {
                "ok": len(new_errors) <= errors_before,
                "updated": n,
                "category": cat,
                "errors": new_errors,
            }

    def set_tags(self, target: str, tags: list[str]) -> dict:
        """Replace the beancount tag set on a transaction. `target` is a txn id
        (a split '~legIdx' suffix is ignored — tags live on the whole entry).
        `tags` are tag names with or without a leading '#'; they are slugified.
        """
        tid, _, _leg = target.partition("~")
        clean = []
        for t in tags:
            s = slug_tag(t)
            if s and s not in clean:
                clean.append(s)

        with self.lock:
            self.ledger.changed()
            errors_before = len(self.ledger.load_errors)
            txn = self._txn_by_id(tid)
            if frozenset(clean) == (txn.tags or frozenset()):
                return {"ok": True, "tags": sorted(clean), "errors": self.errors()}
            new_entry = txn._replace(tags=frozenset(clean))
            _slice, sha = get_entry_slice(txn)
            save_entry_slice(txn, to_string(new_entry, 33, 2), sha)
            self.ledger.load_file()
            self.git_snapshot(f"set tags {tid} -> {sorted(clean)}")
            return {
                "ok": len(self.errors()) <= errors_before,
                "tags": sorted(clean),
                "errors": self.errors(),
            }

    def set_project(self, target: str, project: str | None) -> dict:
        """Set or clear the `project:` posting metadata on one category leg.
        `target` = txn id, optionally 'id~legIdx' for split entries. The name is
        slugified like tags ('Trip Berlin' -> 'trip-berlin'); empty/None clears.
        """
        tid, _, leg = target.partition("~")
        proj = slug_tag(project or "")

        with self.lock:
            self.ledger.changed()
            errors_before = len(self.ledger.load_errors)
            txn = self._txn_by_id(tid)
            _fund, cats = classify_legs(txn)
            k = int(leg) if leg else 0
            if k >= len(cats):
                raise ValueError(f"no category leg {k} on {tid}")

            # An entry-level `project:` acts as a fallback for every leg
            # (leg_project). Materialize it onto the legs before editing so a
            # per-leg set/clear can't be shadowed by the inherited value.
            inherited = slug_tag(str(txn.meta.get("project") or ""))
            postings = list(txn.postings)
            for i, p in cats:
                if inherited and not (p.meta or {}).get("project"):
                    postings[i] = p._replace(
                        meta={**(p.meta or {}), "project": inherited})

            idx, _p = cats[k]
            meta = dict(postings[idx].meta or {})
            if proj:
                meta["project"] = proj
            else:
                meta.pop("project", None)
            postings[idx] = postings[idx]._replace(meta=meta)
            entry_meta = {mk: v for mk, v in txn.meta.items() if mk != "project"}
            new_entry = txn._replace(postings=postings, meta=entry_meta)

            if to_string(new_entry, 33, 2) == to_string(txn, 33, 2):
                return {"ok": True, "project": proj or None, "errors": self.errors()}
            _slice, sha = get_entry_slice(txn)
            save_entry_slice(txn, to_string(new_entry, 33, 2), sha)
            self.ledger.load_file()
            self.git_snapshot(f"set project {target} -> {proj or '(none)'}")
            return {
                "ok": len(self.errors()) <= errors_before,
                "project": proj or None,
                "errors": self.errors(),
            }

    def split_txn(self, target: str, legs: list[dict]) -> dict:
        """Replace ALL category legs of a transaction with a new set of legs —
        the split editor submits the full picture, so this covers splitting,
        re-splitting, and merging back (a single leg) in one operation.

        `legs`: [{"amount": positive magnitude, "category": human path,
        "project": optional name}]. Amounts are in the entry's category-leg
        currency and must sum exactly to the current category total, so the
        entry keeps balancing; the funding leg(s) are never touched. The
        entity leaf (payee-derived) is carried over from the current legs.
        """
        tid, _, _leg = target.partition("~")
        if not legs:
            raise ValueError("no legs")

        with self.lock:
            self.ledger.changed()
            errors_before = len(self.ledger.load_errors)
            txn = self._txn_by_id(tid)
            _fund, cats = classify_legs(txn)
            if not cats:
                raise ValueError(f"{tid} has no category legs")
            ps = [p for _i, p in cats]
            if any(p.cost or p.price for p in ps):
                raise ValueError("cannot split legs carrying cost/price")
            if len({p.units.currency for p in ps}) > 1:
                raise ValueError("cannot split: category legs in multiple currencies")
            roots = {p.account.split(":")[0] for p in ps}
            if len(roots) > 1:
                raise ValueError("cannot split: mixed Income/Expenses legs")
            total = sum(p.units.number for p in ps)
            if total == 0:
                raise ValueError("cannot split: category legs sum to zero")
            sign = 1 if total > 0 else -1
            root = roots.pop()
            _r, _c, entity = cat_entity(ps[0].account)

            cent = Decimal("0.01")
            amounts = [Decimal(str(l.get("amount", 0))).quantize(cent)
                       for l in legs]
            if any(a <= 0 for a in amounts):
                raise ValueError("leg amounts must be positive")
            if sum(amounts) != abs(total):
                raise ValueError(
                    f"leg amounts sum to {sum(amounts)}, expected {abs(total)}")

            template = ps[0]
            cat_idx = {i for i, _p in cats}
            new_postings = [p for i, p in enumerate(txn.postings)
                            if i not in cat_idx]
            new_accounts = []
            for l, amt in zip(legs, amounts):
                cat = slug_category(str(l.get("category") or ""))
                if not cat:
                    raise ValueError("every leg needs a category")
                acc = f"{root}:{cat}:{entity}" if entity else f"{root}:{cat}"
                proj = slug_tag(str(l.get("project") or ""))
                new_accounts.append(acc)
                new_postings.append(template._replace(
                    account=acc,
                    units=template.units._replace(number=sign * amt),
                    meta={"project": proj} if proj else None,
                ))
            # legs now carry their projects explicitly — an entry-level
            # `project:` fallback would override the unlabelled ones
            entry_meta = {mk: v for mk, v in txn.meta.items() if mk != "project"}
            new_entry = txn._replace(postings=new_postings, meta=entry_meta)

            self._ensure_opens(new_accounts)
            _slice, sha = get_entry_slice(txn)
            save_entry_slice(txn, to_string(new_entry, 33, 2), sha)
            self.ledger.load_file()
            self.git_snapshot(f"split {tid} into {len(legs)} legs")
            return {
                "ok": len(self.errors()) <= errors_before,
                "legs": len(legs),
                "errors": self.errors(),
            }

    def delete_txn(self, target: str) -> dict:
        tid, _, leg = target.partition("~")
        if leg:
            raise ValueError("cannot delete a single split leg")
        with self.lock:
            self.ledger.changed()
            txn = self._txn_by_id(tid)
            _slice, sha = get_entry_slice(txn)
            delete_entry_slice(txn, sha)
            self.ledger.load_file()
            self.git_snapshot(f"delete txn {tid}")
            return {"ok": True, "errors": self.errors()}

    def categories(self) -> list[dict]:
        cats = {}
        for open_e in self.ledger.all_entries_by_type.Open:
            acc = open_e.account
            if acc.startswith(CAT_ROOTS):
                root, cat, _e = cat_entity(acc)
                if cat:
                    cats[f"{root}:{cat}"] = {"side": root, "category": cat}
        return [{"id": k, **v} for k, v in sorted(cats.items())]
