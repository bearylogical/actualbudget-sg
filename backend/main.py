from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import pandas as pd
import io
import subprocess
import tempfile
import os
import httpx
from parsers import detect_and_parse

BRIDGE_URL = os.getenv("ACTUAL_BRIDGE_URL", "http://actual-bridge:3001")

app = FastAPI(title="Budget Parser API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def to_xlsx(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """Convert uploaded file to DataFrame, handling .xls via LibreOffice."""
    suffix = ".xlsx" if filename.endswith(".xlsx") else ".xls"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        if suffix == ".xls":
            out_dir = tempfile.mkdtemp()
            subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "xlsx",
                 tmp_path, "--outdir", out_dir],
                capture_output=True, check=True
            )
            xlsx_path = os.path.join(
                out_dir, os.path.basename(tmp_path).replace(".xls", ".xlsx")
            )
        else:
            xlsx_path = tmp_path
        return pd.read_excel(xlsx_path, header=None)
    finally:
        os.unlink(tmp_path)


@app.post("/parse")
async def parse_statement(file: UploadFile = File(...)):
    if not file.filename.endswith((".xls", ".xlsx")):
        raise HTTPException(400, "Only .xls or .xlsx files supported")
    content = await file.read()
    try:
        df = to_xlsx(content, file.filename)
        transactions, bank = detect_and_parse(df, hint=file.filename)
        return {"transactions": transactions, "count": len(transactions), "bank": bank}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/export/csv")
async def export_csv(data: dict):
    transactions = data.get("transactions", [])
    if not transactions:
        raise HTTPException(400, "No transactions provided")
    df = pd.DataFrame(transactions)[
        ["date", "description", "category", "amount", "currency", "is_credit"]
    ]
    df.columns = ["Date", "Description", "Category", "Amount", "Currency", "IsCredit"]
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
        raise HTTPException(r.status_code, r.json().get("error", r.text))
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
