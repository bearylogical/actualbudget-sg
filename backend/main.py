from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
import pandas as pd
import io
import os
import httpx
from parsers import parse_bytes, SUPPORTED_EXTENSIONS
from actual_rules import ActualContext
from pipeline import enrich
from llm import LLMCategorizer
from rules_audit import audit, to_markdown
from taxonomy import TAXONOMY, CATEGORIES, load_aliases, save_aliases
import health

BRIDGE_URL = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")

app = FastAPI(title="Budget Parser API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_context() -> ActualContext | None:
    """Live categories/payees/rules from the loaded Actual budget, or None."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"{BRIDGE_URL}/context")
        if r.is_success:
            return ActualContext.from_bridge(r.json())
    except httpx.HTTPError:
        pass
    return None


async def require_context() -> ActualContext:
    ctx = await get_context()
    if ctx is None:
        raise HTTPException(
            400, "No Actual budget loaded — connect and load a budget first"
        )
    return ctx


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract PDF text, preserving column layout so statement rows stay intact."""
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as e:
        raise HTTPException(400, f"Could not read PDF: {e}")

    if reader.is_encrypted:
        try:
            unlocked = bool(reader.decrypt(""))
        except Exception:
            unlocked = False
        if not unlocked:
            raise HTTPException(
                400,
                "This PDF is password-protected. Please remove the password and re-upload.",
            )

    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(
                ""
            )  # a page with no extractable text shouldn't sink the upload
    text = "\n".join(pages)

    if not text.strip():
        raise HTTPException(
            400,
            "No text found in this PDF — it looks like a scan or an image. "
            "Only text-based statements can be parsed.",
        )
    return text


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/health/live")
async def health_live():
    """Process is up. Used by the Docker healthcheck — no dependencies checked."""
    return {"ok": True}


@app.get("/health")
async def health_full(llm: str = "", strict: bool = False):
    """All services. ?llm=refresh re-tests the LLM now; ?strict=1 returns 503 unless ok."""
    report = await health.full_report(BRIDGE_URL, refresh_llm=(llm == "refresh"))
    code = 503 if strict and report["status"] != "ok" else 200
    return JSONResponse(report, status_code=code)


@app.post("/parse")
async def parse_statement(
    file: UploadFile = File(...), account_id: str = "", use_llm: bool = True
):
    name = (file.filename or "").lower()
    if not name.endswith(SUPPORTED_EXTENSIONS):
        raise HTTPException(400, f"Supported files: {', '.join(SUPPORTED_EXTENSIONS)}")
    content = await file.read()
    try:
        transactions, bank = await run_in_threadpool(
            parse_bytes, content, file.filename or ""
        )
    except Exception as e:
        raise HTTPException(422, f"Could not parse statement: {e}")
    ctx = await get_context()
    result = await run_in_threadpool(
        enrich, transactions, ctx, account_id=account_id or None, use_llm=use_llm
    )
    return {
        **result,
        "count": len(result["transactions"]),
        "bank": bank,
        "actual_connected": ctx is not None,
    }


@app.post("/categorize")
async def categorize_transactions(body: dict):
    """Re-run categorisation, e.g. after connecting to Actual or changing aliases."""
    ctx = await get_context()
    result = await run_in_threadpool(
        enrich,
        body.get("transactions", []),
        ctx,
        account_id=body.get("account_id") or None,
        use_llm=body.get("use_llm", True),
    )
    return {**result, "actual_connected": ctx is not None}


@app.get("/taxonomy")
async def taxonomy():
    return {"groups": TAXONOMY, "categories": CATEGORIES}


@app.get("/aliases")
async def get_aliases():
    return {"aliases": load_aliases()}


@app.post("/aliases")
async def set_aliases(body: dict):
    """body: {"aliases": {canonical: actual category name or id}} — merged into existing."""
    merged = {**load_aliases(), **(body.get("aliases") or {})}
    merged = {k: v for k, v in merged.items() if v}
    save_aliases(merged)
    return {"aliases": merged}


@app.get("/llm/status")
async def llm_status():
    return LLMCategorizer().status()


# ── Rules audit / sync ───────────────────────────────────────────────────────


@app.post("/rules/audit")
async def rules_audit(body: dict | None = None):
    body = body or {}
    ctx = await require_context()
    report = audit(
        ctx, body.get("sample_descriptions"), load_aliases(), bool(body.get("all_seed"))
    )
    return {"report": report, "markdown": to_markdown(report)}


@app.get("/rules/audit.md", response_class=PlainTextResponse)
async def rules_audit_md():
    ctx = await require_context()
    return to_markdown(audit(ctx, None, load_aliases()))


@app.post("/rules/sync")
async def rules_sync(body: dict):
    """
    body: {
      dryRun: bool (default true),
      parts: {aliases, categories, merge_payees, rules, duplicates, broken}  (bools),
      sample_descriptions: [...], all_seed: bool
    }
    """
    dry = body.get("dryRun", True)
    parts = body.get("parts") or {}
    ctx = await require_context()
    report = audit(
        ctx, body.get("sample_descriptions"), load_aliases(), bool(body.get("all_seed"))
    )
    plan = report["plan"]
    selected = {
        "aliases": plan["aliases"] if parts.get("aliases") else {},
        "createCategories": plan["create_categories"]
        if parts.get("categories")
        else [],
        "mergePayees": plan["merge_payees"] if parts.get("merge_payees") else [],
        "createRules": plan["create_rules"] if parts.get("rules") else [],
        "deleteRules": (plan["delete_rules"] if parts.get("duplicates") else [])
        + (plan["delete_broken_rules"] if parts.get("broken") else []),
    }
    if not parts.get("categories"):
        # rules that need a not-yet-created category can't be applied
        selected["createRules"] = [
            r
            for r in selected["createRules"]
            if not any(
                isinstance(a.get("value"), dict) and "$category" in a["value"]
                for a in r["actions"]
            )
        ]
    counts = {k: len(v) for k, v in selected.items()}
    if dry:
        return {"dryRun": True, "counts": counts, "plan": selected}

    if selected["aliases"]:
        save_aliases({**load_aliases(), **selected["aliases"]})
    result = {"ok": True}
    if any(
        selected[k]
        for k in ("createCategories", "mergePayees", "createRules", "deleteRules")
    ):
        body_out = {
            k: selected[k]
            for k in ("createCategories", "mergePayees", "createRules", "deleteRules")
        }
        result = await _bridge("POST", "/rules/apply", body_out, timeout=180)
    after = await require_context()
    after_report = audit(
        after,
        body.get("sample_descriptions"),
        load_aliases(),
        bool(body.get("all_seed")),
    )
    return {
        "dryRun": False,
        "counts": counts,
        "result": result,
        "summary_after": after_report["summary"],
    }


@app.post("/export/csv")
async def export_csv(data: dict):
    transactions = data.get("transactions", [])
    if not transactions:
        raise HTTPException(400, "No transactions provided")
    df = pd.DataFrame(transactions)
    for col in ("payee", "category", "currency", "is_credit"):
        if col not in df.columns:
            df[col] = ""
    df = df[
        ["date", "payee", "description", "category", "amount", "currency", "is_credit"]
    ]
    df.columns = [
        "Date",
        "Payee",
        "Description",
        "Category",
        "Amount",
        "Currency",
        "IsCredit",
    ]
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=budget_export.csv"},
    )


# ── Actual Budget bridge proxy ────────────────────────────────────────────────


async def _bridge(method: str, path: str, body: dict = None, timeout: int = 30):
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method == "GET":
            r = await client.get(f"{BRIDGE_URL}{path}")
        else:
            r = await client.post(f"{BRIDGE_URL}{path}", json=body or {})
    if not r.is_success:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        raise HTTPException(r.status_code, detail)
    return r.json()


@app.post("/actual/budgets")
async def actual_list_budgets(body: dict):
    return await _bridge("POST", "/budgets", body)


@app.post("/actual/budgets/load")
async def actual_load_budget(body: dict):
    return await _bridge("POST", "/budgets/load", body, timeout=60)


@app.get("/actual/accounts")
async def actual_accounts():
    return await _bridge("GET", "/accounts")


@app.get("/actual/categories")
async def actual_categories():
    return await _bridge("GET", "/categories")


@app.post("/actual/categories")
async def actual_create_category(body: dict):
    return await _bridge("POST", "/categories", body)


@app.get("/actual/payees")
async def actual_payees():
    return await _bridge("GET", "/payees")


@app.get("/actual/rules")
async def actual_rules():
    return await _bridge("GET", "/rules")


@app.post("/actual/rules")
async def actual_create_rules(body: dict):
    return await _bridge("POST", "/rules", body)


@app.post("/actual/preview")
async def actual_preview(body: dict):
    return await _bridge("POST", "/preview", body)


@app.get("/actual/budget-month/{month}")
async def actual_budget_month(month: str):
    return await _bridge("GET", f"/budget-month/{month}")


@app.post("/actual/import")
async def actual_import(body: dict):
    return await _bridge("POST", "/import", body, timeout=60)


@app.post("/actual/reset")
async def actual_reset():
    return await _bridge("POST", "/reset")
