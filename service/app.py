"""zerro-bean-service: FastAPI JSON API + static host for the reworked Zerro UI.

Run:  .venv/bin/python -m uvicorn app:app --app-dir service --host 0.0.0.0 --port 5113
Env:  LEDGER (path to main.beancount), STATIC_DIR (frontend dist), PORT
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from beanledger import BeanLedger

LEDGER = os.environ.get(
    "LEDGER",
    str(Path(__file__).resolve().parent.parent
        / "ledger-plinsburg/plinsburg-ledger/main.beancount"),
)
STATIC_DIR = os.environ.get(
    "STATIC_DIR",
    str(Path(__file__).resolve().parent.parent / "repos/zerro/dist"),
)

app = FastAPI(title="zerro-bean-service")
bl = BeanLedger(LEDGER)


class CategorizeBody(BaseModel):
    scope: str  # "txn" | "entity"
    target: str
    category: str
    side: str = "Expenses"  # entity scope only: which side's legs to rewrite
    applyToFuture: bool = False
    force: bool = False


class SetTagsBody(BaseModel):
    target: str
    tags: list[str]


class SetProjectBody(BaseModel):
    target: str  # txn id, optionally 'id~legIdx'
    project: str | None = None  # empty/None clears


class CreateProjectBody(BaseModel):
    name: str


class SplitLeg(BaseModel):
    amount: float  # positive magnitude in the entry's category currency
    category: str  # human path, slugified server-side
    project: str | None = None


class SplitBody(BaseModel):
    target: str  # txn id ('~legIdx' suffix ignored — splits edit the whole entry)
    legs: list[SplitLeg]


class DeleteBody(BaseModel):
    id: str


@app.get("/api/health")
def health():
    return {"ok": True, "ledger": LEDGER}


@app.get("/api/zm-diff")
def zm_diff():
    return bl.build_zm_diff()


@app.get("/api/changed")
def changed():
    return {"changed": bl.changed(), "mtime": str(bl.ledger.mtime)}


@app.get("/api/validate")
def validate():
    errs = bl.errors()
    return {"ok": not errs, "errors": errs}


@app.get("/api/categories")
def categories():
    return bl.categories()


@app.post("/api/categorize")
def categorize(body: CategorizeBody):
    try:
        return bl.categorize(body.scope, body.target, body.category,
                             body.applyToFuture, body.side, body.force)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/set-tags")
def set_tags(body: SetTagsBody):
    try:
        return bl.set_tags(body.target, body.tags)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/set-project")
def set_project(body: SetProjectBody):
    try:
        return bl.set_project(body.target, body.project)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/create-project")
def create_project(body: CreateProjectBody):
    try:
        return bl.create_project(body.name)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/split")
def split(body: SplitBody):
    try:
        return bl.split_txn(body.target, [l.model_dump() for l in body.legs])
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/delete")
def delete(body: DeleteBody):
    try:
        return bl.delete_txn(body.id)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- static frontend (SPA) ------------------------------------------------
static = Path(STATIC_DIR)
if static.is_dir():
    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (static / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(static.resolve()):
            return FileResponse(candidate)
        return FileResponse(static / "index.html")
else:
    @app.get("/")
    def root():
        return JSONResponse({"service": "zerro-bean-service", "static": "not built yet"})
