# zerro-bean-service

FastAPI backend for the reworked Zerro UI, reusing `fava.core` as a library
(SPEC §5, recommendation B from `analysis/fava.md`).

## Endpoints
| endpoint | what |
|---|---|
| `GET /api/zm-diff` | full dataset in Zenmoney `TDiff` shape (instruments, user, accounts, tags-as-categories, merchants-as-entities, 2-leg transactions; splits → one pseudo-row per category leg, id `<id>~<legIdx>`) |
| `GET /api/changed` | reload-if-changed poll |
| `GET /api/validate` | current `load_errors` |
| `GET /api/categories` | category list for autocomplete |
| `POST /api/categorize` | `{scope: txn\|entity, target, category, side?, applyToFuture?, force?}` — durable rewrite of category posting account(s) via sha256-guarded `save_entry_slice`; auto-inserts missing `open`s; git-commits |
| `POST /api/delete` | delete a whole transaction (source slice) |
| `GET /*` | serves the built frontend (`repos/zerro/dist`), SPA fallback |

## Safety
- Entity-scope edits **skip `#vrs-reimbursed` transactions and `VRS-Reimbursed` legs** (brief §6) unless `force: true`.
- Entity-scope edits are side-scoped (`Expenses` default) so refunds (`Income:Returns:...`) are untouched.
- Every write batch is validated (ledger reload) and git-committed in the ledger dir.
- All transactions carry stable `id:` metadata (added once by `add_ids.py`, SPEC O2).
- `applyToFuture` upserts `merchant_rules.json` next to the ledger (SPEC §7, for the Phase-5 importer).

## Run
Deployed as a systemd user service (`~/.config/systemd/user/zerro-bean.service`,
linger enabled — survives logout/reboot):

```
systemctl --user {status,restart,stop} zerro-bean
```

Manual: `LEDGER=... STATIC_DIR=... .venv/bin/python -m uvicorn app:app --app-dir service --host 0.0.0.0 --port 5113`
