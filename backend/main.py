from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
import pandas as pd
import io
import os
import httpx
from parsers import parse_statement as parse_statement_bytes, SUPPORTED_EXTENSIONS
import accounts as acct
from actual_rules import ActualContext
from pipeline import enrich
import history
from llm import LLMCategorizer
from rules_audit import audit, to_markdown
from taxonomy import TAXONOMY, CATEGORIES, load_aliases, save_aliases
import health
import connection
import import_log
import bridge_client

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
    upload_id = await run_in_threadpool(import_log.start, "ui", file.filename or "statement", content)
    try:
        transactions, bank, info = await run_in_threadpool(
            parse_statement_bytes, content, file.filename or ""
        )
    except Exception as e:
        await run_in_threadpool(import_log.fail, upload_id, "parse", e)
        raise HTTPException(422, f"Could not parse statement: {e}")
    await run_in_threadpool(import_log.set_statement, upload_id, info, len(transactions))
    ctx = await get_context()
    account = await recommend_account(info, transactions) if ctx else _no_recommendation(info)
    # an explicit choice wins; otherwise use a confident recommendation so account-scoped
    # Actual rules are previewed correctly
    use_account = account_id or (account["recommended"] if account.get("auto_select") else None)
    hist = await run_in_threadpool(history.get, ctx)
    result = await run_in_threadpool(
        enrich, transactions, ctx, account_id=use_account, use_llm=use_llm, history=hist
    )
    return {
        **result,
        "count": len(result["transactions"]),
        "bank": bank,
        "statement": info.to_dict(),
        "account": account,
        "actual_connected": ctx is not None,
        "upload_id": upload_id,
        # the exact same file (SHA-256) seen before — imported, undone or abandoned
        "seen_before": import_log.seen_before(import_log.sha256(content), exclude=upload_id),
    }


def _no_recommendation(info) -> dict:
    return {"statement": info.to_dict(), "suggestions": [], "recommended": None,
            "auto_select": False, "remembered": acct.load_map().get(info.fingerprint, {}).get("account_id")}


async def recommend_account(info, transactions: list[dict]) -> dict:
    """Rank Actual accounts for this statement (see accounts.py)."""
    try:
        data = await _bridge("POST", "/accounts/match", acct.match_request(transactions), timeout=90)
    except HTTPException:
        return _no_recommendation(info)
    rows = data.get("accounts", [])
    return acct.recommend(info, transactions, rows, acct.match_counts(transactions, rows))


@app.post("/accounts/recommend")
async def accounts_recommend(body: dict):
    """Re-rank after connecting to Actual: {statement, transactions}."""
    info = acct.StatementInfo.from_dict(body.get("statement"))
    if await get_context() is None:
        return _no_recommendation(info)
    return await recommend_account(info, body.get("transactions") or [])


@app.post("/accounts/remember")
async def accounts_remember(body: dict):
    """After a successful import: {fingerprint, account_id, account_name}."""
    if not body.get("fingerprint") or not body.get("account_id"):
        raise HTTPException(400, "fingerprint and account_id required")
    return {"map": acct.remember(body["fingerprint"], body["account_id"], body.get("account_name", ""))}


@app.get("/accounts/map")
async def accounts_map():
    return {"map": acct.load_map()}


@app.post("/accounts/forget")
async def accounts_forget(body: dict):
    return {"map": acct.forget(body.get("fingerprint", ""))}


@app.post("/categorize")
async def categorize_transactions(body: dict):
    """Re-run categorisation, e.g. after connecting to Actual or changing aliases.
    {refresh: true} rebuilds your category history from Actual first (the Refresh button)."""
    ctx = await get_context()
    hist = await run_in_threadpool(history.get, ctx, bool(body.get("refresh")))
    result = await run_in_threadpool(
        enrich,
        body.get("transactions", []),
        ctx,
        account_id=body.get("account_id") or None,
        use_llm=body.get("use_llm", True),
        history=hist,
    )
    return {**result, "actual_connected": ctx is not None,
            "history_size": len(hist) if hist is not None else 0}


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


async def _bridge(method: str, path: str, body: dict = None, timeout: int = 30, _retry: bool = True):
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                r = await client.get(f"{BRIDGE_URL}{path}")
            else:
                r = await client.post(f"{BRIDGE_URL}{path}", json=body or {})
    except httpx.TimeoutException:
        raise HTTPException(504, f"actual-bridge did not answer within {timeout}s — it may be stuck "
                                 "reaching your Actual server (DNS / TLS / firewall from inside Docker?)")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Can't reach actual-bridge at {BRIDGE_URL} ({type(e).__name__}) — "
                                 "is the container running? `docker compose logs actual-bridge`")
    if not r.is_success:
        try:
            detail = r.json().get("error", r.text)
        except ValueError:
            detail = r.text
        # bridge restarted and lost its in-memory budget → reload the saved connection, retry once
        if _retry and bridge_client.is_no_budget(r.status_code, detail) \
                and await run_in_threadpool(bridge_client.reload_saved_budget):
            return await _bridge(method, path, body, timeout, _retry=False)
        raise HTTPException(r.status_code, detail)
    return r.json()


@app.post("/actual/budgets")
async def actual_list_budgets(body: dict):
    return await _bridge("POST", "/budgets", body)


@app.post("/actual/budgets/load")
async def actual_load_budget(body: dict):
    data = await _bridge("POST", "/budgets/load", body, timeout=60)
    # Share this connection with the scheduler (see connection.py) so it needs no compose config
    try:
        connection.save(body)
    except Exception as e:
        print(f"[connection] could not save scheduler config: {e}")
    return data


@app.get("/scheduler/connection")
async def scheduler_connection():
    """What the scheduler will use (passwords omitted) + the account map it routes with."""
    return {**connection.status(), "account_map": acct.load_map()}


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
    """Forward to the bridge. uploadId / accountName (from /parse) record the result in the history."""
    upload_id = body.pop("uploadId", None)
    account_name = body.pop("accountName", "")
    dry = bool(body.get("dryRun"))
    try:
        data = await _bridge("POST", "/import", body, timeout=60)
    except HTTPException as e:
        if upload_id and not dry:
            await run_in_threadpool(import_log.fail, upload_id, "import", e.detail)
        raise
    if upload_id and not dry:
        await run_in_threadpool(import_log.record_result, upload_id, data, body.get("accountId", ""),
                                account_name, "chosen in the web UI")
    return data


@app.post("/actual/undo-import")
async def actual_undo_import(body: dict):
    """Delete the rows an import just added (ids from its addedIds). Actual keeps deleted
    imported_ids per account, so re-importing the same file into this account skips them."""
    ids = [i for i in (body.get("ids") or []) if isinstance(i, str)]
    if not ids:
        raise HTTPException(400, "ids required")
    data = await _bridge("POST", "/txns/delete", {"ids": ids}, timeout=60)
    if body.get("uploadId"):
        await run_in_threadpool(import_log.update, body["uploadId"], status="undone")
    return data


@app.post("/actual/reset")
async def actual_reset():
    """Disconnect: also forget the saved connection, so the scheduler stops importing."""
    try:
        connection.clear()
    except Exception as e:
        print(f"[connection] could not clear scheduler config: {e}")
    return await _bridge("POST", "/reset")


# ── Review queue (duplicates / transfers / reconciliation fixes) ─────────────
import review
import reconcile
from datetime import date as _date, timedelta as _td


def _account_last4() -> dict[str, str]:
    """account_id → last 4 digits, from remembered statement fingerprints."""
    out = {}
    for fp, v in acct.load_map().items():
        last4 = fp.rsplit("|", 1)[-1]
        if last4 and last4 != "?":
            out[v["account_id"]] = last4
    return out


def _apply(item: dict) -> dict:
    try:
        actions = bridge_client.actions_for(item)
        result = bridge_client.apply_actions(actions) if actions else {"ok": True, "results": [], "errors": []}
        review.mark_done(item["id"], result, ok=result["ok"])
        if result["ok"]:
            result["superseded"] = review.supersede(bridge_client.touched_ids(actions), except_id=item["id"])
    except Exception as e:
        result = {"ok": False, "errors": [str(e)]}
        review.mark_done(item["id"], result, ok=False)
    return {**review.get(item["id"]), "applied": result}


def scan_and_queue(start: str, end: str, account_ids: list[str] | None = None) -> dict:
    """Find duplicates + unlinked transfers in Actual and queue them. Learned items apply themselves."""
    rows, _ = bridge_client.txns(start, end)
    if account_ids:
        dup_rows = [t for t in rows if t["account"] in account_ids]
    else:
        dup_rows = rows
    queued, auto = [], []
    for d in review.find_existing_duplicates(dup_rows):
        it = review.enqueue("existing_duplicate", {**d, "account": d["a"]["account"]}, [d["a"]["id"], d["b"]["id"]])
        (auto if it["status"] == "auto" else queued).append(it)
    for p in review.find_transfer_pairs(rows, _account_last4()):
        if account_ids and not ({p["a"]["account"], p["b"]["account"]} & set(account_ids)):
            continue
        it = review.enqueue("transfer_pair", p, [p["a"]["id"], p["b"]["id"]])
        (auto if it["status"] == "auto" else queued).append(it)
    applied = [_apply(it) for it in auto if it.get("decision")]
    return {"queued": len([q for q in queued if q["status"] == "pending"]), "auto_applied": len(applied),
            "items": queued + applied}


def recategorize_scan(start: str, end: str, mode: str = "uncategorised", use_llm: bool = True,
                      account_ids: list[str] | None = None) -> dict:
    """Propose categories for rows already in Actual and queue them. Learned patterns apply themselves."""
    import recategorize
    ctx = ActualContext.from_bridge(bridge_client.call("GET", "/context", timeout=60))
    rows, _ = bridge_client.txns(start, end, account_ids)
    hist = history.get(ctx, refresh=True)
    proposals = recategorize.propose(rows, ctx, mode=mode, use_llm=use_llm, history=hist)
    queued, auto = [], []
    for p in proposals:
        it = review.enqueue("recategorize", p, [p["txn"]["id"], p["proposed"]["id"]])
        if it["status"] == "auto":
            auto.append(it)
        elif it["status"] == "pending":
            queued.append(it)
    applied = _apply_recategorize(auto)
    return {"scanned": len(recategorize.candidates(rows)), "proposed": len(proposals),
            "queued": len(queued), "auto_applied": len(applied)}


def _apply_recategorize(items: list[dict]) -> list[dict]:
    """Apply decided recategorize items in ONE bridge call (hundreds of rows → one sync)."""
    import recategorize
    todo = [it for it in items if it.get("decision") == "apply"]
    for it in items:
        if it.get("decision") == "keep":
            review.mark_done(it["id"], {"ok": True, "results": [], "errors": []})
    if not todo:
        return []
    try:
        r = bridge_client.call("POST", "/txns/update",
                               {"updates": [recategorize.update_for(it["payload"]) for it in todo]}, timeout=300)
        failed = {e.split(":", 1)[0] for e in r.get("errors") or []}
    except Exception as e:
        r, failed = {"errors": [str(e)]}, {it["payload"]["txn"]["id"] for it in todo}
    for it in todo:
        bad = it["payload"]["txn"]["id"] in failed
        review.mark_done(it["id"], {"ok": not bad, "errors": [x for x in r.get("errors") or []
                                                              if x.startswith(it["payload"]["txn"]["id"])] or
                                    (r.get("errors") if bad else [])}, ok=not bad)
    return todo


@app.post("/review/recategorize/scan")
async def review_recategorize_scan(body: dict | None = None):
    """{days: 120, mode: "uncategorised"|"all", use_llm: true} or {start, end, account_ids}"""
    body = body or {}
    end = body.get("end") or _date.today().isoformat()
    start = body.get("start") or (_date.fromisoformat(end) - _td(days=int(body.get("days", 120)))).isoformat()
    try:
        return await run_in_threadpool(recategorize_scan, start, end, body.get("mode", "uncategorised"),
                                       body.get("use_llm", True), body.get("account_ids"))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))


@app.get("/review")
async def review_list(status: str = "pending", kind: str = ""):
    return {"items": review.list_items(status or None, kind or None), "counts": review.counts()}


@app.get("/review/counts")
async def review_counts():
    return review.counts()


@app.post("/review/scan")
async def review_scan(body: dict | None = None):
    """{days: 90} or {start, end, account_ids}"""
    body = body or {}
    end = body.get("end") or _date.today().isoformat()
    start = body.get("start") or (_date.fromisoformat(end) - _td(days=int(body.get("days", 90)))).isoformat()
    try:
        res = await run_in_threadpool(scan_and_queue, start, end, body.get("account_ids"))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    if body.get("uploadId"):
        await run_in_threadpool(import_log.add_review_ids, body["uploadId"], [i["id"] for i in res["items"]])
    return res


@app.post("/review/{item_id}/decide")
async def review_decide(item_id: str, body: dict):
    """Approve a decision; it is applied to Actual immediately and remembered.
    recategorize items also take {category_id} to apply a different category than proposed."""
    if body.get("category_id"):
        it = review.get(item_id)
        if not it:
            raise HTTPException(404, "no such item")
        if it["kind"] != "recategorize":
            raise HTTPException(400, "category_id only applies to recategorize items")
        ctx = ActualContext.from_bridge(await _bridge("GET", "/context"))
        _override_category(it, ctx, body["category_id"])
    try:
        item = review.decide(item_id, body.get("decision", ""), by="you")
    except KeyError:
        raise HTTPException(404, "no such item")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(_apply, item)


def _override_category(it: dict, ctx: ActualContext, category_id: str):
    cat = ctx.cat_by_id.get(category_id)
    if not cat:
        raise HTTPException(400, "unknown category_id")
    it["payload"]["proposed"] = {**it["payload"]["proposed"], "id": cat["id"], "name": cat["name"],
                                 "group": cat.get("group_name"), "source": "manual", "confidence": 1.0}
    review.set_payload(it["id"], it["payload"])


@app.post("/review/recategorize/apply-many")
async def review_recategorize_apply_many(body: dict):
    """{ids, category_id?} — approve several category fixes at once (e.g. every waiting row for
    one payee), optionally with a different category. Each counts as your decision, so the
    payee → category pattern is learned. Applied to Actual in one call."""
    ids = body.get("ids") or []
    ctx = ActualContext.from_bridge(await _bridge("GET", "/context")) if body.get("category_id") else None
    decided = []
    for iid in ids:
        it = review.get(iid)
        if not it or it["kind"] != "recategorize" or it["status"] != "pending":
            continue
        if ctx is not None:
            _override_category(it, ctx, body["category_id"])
        decided.append(review.decide(iid, "apply", by="you"))
    done = await run_in_threadpool(_apply_recategorize, decided)
    failed = [review.get(it["id"]) for it in done]
    return {"applied": sum(1 for it in failed if it and it["status"] == "done"),
            "failed": [it["id"] for it in failed if it and it["status"] == "failed"]}


def recategorize_refresh(use_llm: bool = True) -> dict:
    """Re-check waiting category fixes against what you've taught since they were queued:
    drop ones you've fixed in Actual meanwhile, update ones whose best category changed
    (your rules, mappings or history), and apply ones matching a pattern you've now approved
    enough times. Nothing else is written to Actual."""
    import recategorize
    pending = review.list_items("pending", "recategorize", limit=5000)
    fixed_elsewhere = changed = 0
    if pending:
        ctx = ActualContext.from_bridge(bridge_client.call("GET", "/context", timeout=60))
        hist = history.get(ctx, refresh=True)
        dates = sorted(it["payload"]["txn"]["date"] for it in pending)
        rows, _ = bridge_client.txns(dates[0], dates[-1])
        by_id = {r["id"]: r for r in rows}
        still = []
        for it in pending:
            t = it["payload"]["txn"]
            r = by_id.get(t["id"])
            if r is None or r.get("category") != (it["payload"]["current"] or {}).get("id"):
                review.set_status(it["id"], "superseded")    # deleted, or categorised in Actual
                fixed_elsewhere += 1
            else:
                still.append((it, r))
        props = {p["txn"]["id"]: p for p in
                 recategorize.propose([r for _, r in still], ctx, use_llm=use_llm, history=hist)}
        for it, r in still:
            p = props.get(r["id"])
            if not p:
                continue            # no better idea now; keep the suggestion you already have
            if p["proposed"]["id"] != it["payload"]["proposed"]["id"]:
                review.set_status(it["id"], "superseded")
                review.enqueue("recategorize", p, [p["txn"]["id"], p["proposed"]["id"]])
                changed += 1
            elif p["proposed"] != it["payload"]["proposed"]:
                review.set_payload(it["id"], p)             # same category, fresher reason/confidence
    review.promote_learned("recategorize")
    auto = [it for it in review.list_items("auto", "recategorize", limit=5000)]
    applied = _apply_recategorize(auto)
    return {"checked": len(pending), "fixed_elsewhere": fixed_elsewhere, "changed": changed,
            "auto_applied": len(applied), "pending": review.counts().get("recategorize", {}).get("pending", 0)}


@app.post("/review/recategorize/refresh")
async def review_recategorize_refresh(body: dict | None = None):
    try:
        return await run_in_threadpool(recategorize_refresh, (body or {}).get("use_llm", True))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))


@app.post("/review/{item_id}/dismiss")
async def review_dismiss(item_id: str):
    review.dismiss(item_id)
    return review.get(item_id)


@app.post("/review/advise")
async def review_advise(body: dict | None = None):
    """Ask the LLM for a verdict on pending items (advisory only)."""
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    ids = set((body or {}).get("ids") or [])
    items = [i for i in review.list_items("pending") if (not ids or i["id"] in ids)
             and i["kind"] not in ("reconcile_fix", "recategorize")][:40]
    verdicts = await run_in_threadpool(review.advise, items, llm)
    return {"advised": len(verdicts), "items": review.list_items("pending")}


@app.post("/review/accept-ai")
async def review_accept_ai(body: dict | None = None):
    """Bulk-approve pending items whose AI verdict is at least min_confidence (default 0.8).
    Deletions are never bulk-approved."""
    min_conf = float((body or {}).get("min_confidence", 0.8))
    done = []
    # category fixes: the proposal's own confidence; applied together in one bridge call
    recat = [review.decide(it["id"], "apply", by="you") for it in review.list_items("pending", "recategorize")
             if it["payload"]["proposed"].get("confidence", 0) >= min_conf]
    done += await run_in_threadpool(_apply_recategorize, recat)
    for it in review.list_items("pending"):
        if it["kind"] == "recategorize":
            continue
        d = review.suggested_decision(it)
        conf = (it.get("llm") or {}).get("confidence", 0)
        if not d or conf < min_conf or d.startswith("delete"):
            continue
        item = review.decide(it["id"], d, by="you")
        done.append(await run_in_threadpool(_apply, item))
    return {"applied": len(done), "items": done}


@app.get("/review/memory")
async def review_memory():
    return {"memory": review.memory(), "learn_after": review.LEARN_AFTER}


@app.post("/review/memory/forget")
async def review_forget(body: dict):
    review.forget(body.get("key", ""))
    return {"memory": review.memory()}


# ── Reconciliation ───────────────────────────────────────────────────────────

def run_reconcile(account_id: str, info, statement_rows: list[dict], balance: float | None = None,
                  as_of: str | None = None, use_llm: bool = True) -> dict:
    as_of = as_of or info.period_end or max((t["date"] for t in statement_rows), default=_date.today().isoformat())
    start = info.period_start or min((t["date"] for t in statement_rows), default=as_of)
    rows, accts = bridge_client.txns(reconcile.EPOCH, as_of, [account_id])
    others, _ = bridge_client.txns(start, as_of)
    others = [t for t in others if t["account"] != account_id]
    name = next((a["name"] for a in accts if a["id"] == account_id), account_id)
    report = reconcile.analyse(account_id, info, statement_rows, rows, balance, as_of, name, others)
    llm = LLMCategorizer()
    if use_llm and llm.enabled and not report["reconciled"]:
        try:
            ag = reconcile.agent(report, statement_rows, llm)
            return {"report": report, "explanation": ag["explanation"], "tool_calls": ag["tool_calls"],
                    "queued": ag["queued"], "engine": "llm"}
        except Exception as e:   # fall back to the deterministic plan
            report["llm_error"] = str(e)[:300]
    return {"report": report, "explanation": None, "queued": reconcile.queue(report), "engine": "rules"}


@app.post("/reconcile")
async def reconcile_account(body: dict):
    """{account_id, statement?, transactions?, balance?, as_of?, use_llm?} → report + queued fixes."""
    if not body.get("account_id"):
        raise HTTPException(400, "account_id required")
    info = acct.StatementInfo.from_dict(body.get("statement"))
    try:
        res = await run_in_threadpool(run_reconcile, body["account_id"], info, body.get("transactions") or [],
                                      body.get("balance"), body.get("as_of"), body.get("use_llm", True))
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    if body.get("uploadId"):
        await run_in_threadpool(_log_reconcile, body["uploadId"], res)
    return res


def _log_reconcile(upload_id: str, res: dict):
    import_log.update(upload_id, reconcile=import_log.reconcile_summary(res["report"], res.get("queued") or []))
    import_log.add_review_ids(upload_id, [q["id"] for q in res.get("queued") or [] if q.get("id")])


# ── Import history ───────────────────────────────────────────────────────────

@app.get("/imports")
async def imports_list(limit: int = 100, source: str = "", status: str = "", account_id: str = "",
                       abandoned: bool = True):
    """Every statement file uploaded in the UI or dropped in the watch folder, newest first."""
    rows = await run_in_threadpool(import_log.listing, limit, source, status, account_id, abandoned)
    return {"imports": rows, "summary": await run_in_threadpool(import_log.summary)}


@app.get("/imports/{iid}")
async def imports_get(iid: str):
    r = await run_in_threadpool(import_log.get, iid)
    if not r:
        raise HTTPException(404, "No such import")
    if r["review_ids"]:
        statuses = await run_in_threadpool(review.statuses, r["review_ids"])
        r["review"] = statuses
    return r


@app.get("/imports/{iid}/file")
async def imports_file(iid: str):
    from fastapi.responses import FileResponse
    f = await run_in_threadpool(import_log.file_path, iid)
    if not f:
        raise HTTPException(404, "The original file isn't kept for this import")
    return FileResponse(f[0], filename=f[1])


@app.post("/imports/{iid}/undo")
async def imports_undo(iid: str):
    """Delete the rows this import added. Actual remembers deleted imported_ids per account,
    so re-importing the same file into that account won't bring them back silently."""
    r = await run_in_threadpool(import_log.get, iid)
    if not r:
        raise HTTPException(404, "No such import")
    if r["status"] != "imported" or not r["added_ids"]:
        raise HTTPException(409, "Only an import that added rows, and hasn't been undone, can be undone")
    data = await _bridge("POST", "/txns/delete", {"ids": r["added_ids"]}, timeout=60)
    await run_in_threadpool(import_log.update, iid, status="undone")
    return {**data, "import": await run_in_threadpool(import_log.get, iid)}


# ── Money dashboard (Actual + Ghostfolio) ────────────────────────────────────
import finance
import ghostfolio


def _card_account_ids() -> set[str]:
    return {v["account_id"] for fp, v in acct.load_map().items() if "|credit_card|" in fp}


@app.get("/finance/summary")
async def finance_summary(months: int = 6):
    try:
        actual = await run_in_threadpool(bridge_client.call, "GET", f"/finance/summary?months={int(months)}")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    ghost = await run_in_threadpool(ghostfolio.summary)
    return finance.compute(actual, ghost, _card_account_ids(), review.counts().get("pending", 0))


@app.post("/finance/brief")
async def finance_brief():
    llm = LLMCategorizer()
    if not llm.enabled:
        raise HTTPException(400, "LLM is not configured (LLM_PROVIDER)")
    summary = await finance_summary()
    bullets = await run_in_threadpool(finance.brief, summary, llm)
    return {"bullets": bullets, "model": llm.model}


# ── Investments: IBKR → Ghostfolio (on demand, data read by Claude's IBKR connector) ──
import re

import ibkr_ghostfolio as ibgf


def _ibkr_account(account_id: str | None) -> dict:
    if account_id:
        a = next((x for x in ghostfolio.accounts() if x["id"] == account_id), None)
        if not a:
            raise HTTPException(400, f"Ghostfolio has no account {account_id}")
        return a
    a = ghostfolio.find_broker_account()
    if not a:
        raise HTTPException(400, "No IBKR account found in Ghostfolio — create one (platform: Interactive Brokers) "
                                 "or pass account_id")
    return a


@app.post("/investments/ibkr/preview")
async def ibkr_preview(body: dict):
    """{trades: [...], positions: [...], account_id?} — raw IBKR connector output. Writes nothing."""
    def run():
        a = _ibkr_account(body.get("account_id"))
        mapped = ibgf.map_trades(body.get("trades") or [], body.get("positions") or [], a["id"])
        new, dup = ibgf.already_imported(mapped["activities"], ghostfolio.orders(a["id"]))
        check = ghostfolio.import_activities(new, dry_run=True) if new else {"activities": []}
        problems = [x for x in check.get("activities", []) if x.get("error")]
        current = ghostfolio.holdings(a["id"])
        before = ibgf.compare_positions(body.get("positions") or [], current)
        # what would still differ after importing `new` → candidates for opening lots
        after = ibgf.compare_positions(body.get("positions") or [],
                                       current + [{"symbol": x["symbol"], "quantity": x["quantity"] * (1 if x["type"] == "BUY" else -1)}
                                                  for x in new])
        first = min((x["date"][:10] for x in mapped["activities"]), default=_date.today().isoformat())
        opening_date = (_date.fromisoformat(first) - _td(days=1)).isoformat()
        return {"account": {"id": a["id"], "name": a.get("name"), "currency": a.get("currency"),
                            "cash_balance_to_clear": float(a.get("balance") or 0)}, "new": new, "already_imported": len(dup),
                "skipped": mapped["skipped"], "unmapped": mapped["unmapped"], "ghostfolio_check": problems,
                "position_diff_before": before, "position_diff_after_import": after,
                "suggested_opening_lots": ibgf.opening_lots(body.get("positions") or [], after, a["id"], opening_date)}
    try:
        return await run_in_threadpool(run)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.post("/investments/ibkr/import")
async def ibkr_import(body: dict):
    """{activities: [...from preview.new], positions?: [...], account_id?, keep_cash?} — imports into Ghostfolio.

    Every activity is linked to the IBKR account (activities without accountId would land in no account),
    anything already in Ghostfolio is dropped again (safe to re-run), and afterwards the account's cash
    balance is set to 0 so Ghostfolio tracks the stocks/ETFs only — the cash is tracked in Actual."""
    acts = body.get("activities") or []
    if not acts:
        raise HTTPException(400, "nothing to import")
    bad = [a for a in acts if not re.search(r"ibkr:\S+", str(a.get("comment") or ""))]
    if bad:
        raise HTTPException(400, f"{len(bad)} activities lack the ibkr:<trade id> comment — pass the preview's "
                                 "`new` / `suggested_opening_lots` unchanged")
    def run():
        a = _ibkr_account(body.get("account_id") or next((x["accountId"] for x in acts if x.get("accountId")), None))
        linked = [{**x, "accountId": a["id"]} for x in acts]
        new, dup = ibgf.already_imported(linked, ghostfolio.orders(a["id"]))
        res = ghostfolio.import_activities(new, dry_run=False) if new else {"activities": []}
        cleared = [] if body.get("keep_cash") else ghostfolio.clear_cash([a["id"]], dry_run=False)
        diff = ibgf.compare_positions(body.get("positions") or [], ghostfolio.holdings(a["id"])) if body.get("positions") else None
        return {"account": {"id": a["id"], "name": a.get("name")}, "imported": len(res.get("activities", new)),
                "already_imported": len(dup), "cash_cleared": cleared, "position_diff_after": diff}
    try:
        return await run_in_threadpool(run)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")


@app.post("/investments/ghostfolio/clear-cash")
async def ghostfolio_clear_cash(body: dict | None = None):
    """{account_ids?: [...], dry_run?: true} — zero cash balances so Ghostfolio tracks securities only.
    Default is a dry run over all accounts; activities and holdings are never touched."""
    body = body or {}
    try:
        changed = await run_in_threadpool(ghostfolio.clear_cash, body.get("account_ids") or None,
                                          body.get("dry_run", True) is not False)
    except ghostfolio.GhostfolioError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Ghostfolio unreachable: {e}")
    return {"dry_run": body.get("dry_run", True) is not False, "accounts": changed}


@app.get("/finance/week")
async def finance_week(days: int = 7):
    """Last N days of spending vs your usual week (previous 4 weeks)."""
    today = _date.today()
    start = (today - _td(days=days - 1 + 28)).isoformat()
    try:
        rows, accts = await run_in_threadpool(bridge_client.txns, start, today.isoformat())
        ctx = await run_in_threadpool(bridge_client.call, "GET", "/context")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    names = {c["id"]: c["name"] for c in ctx.get("categories", [])}
    return finance.week(rows, names, today, days)


@app.get("/finance/transactions")
async def finance_transactions(start: str = "", end: str = "", q: str = "", account: str = "",
                               category: str = "", kind: str = "all", limit: int = 200):
    """Historical Actual transactions, filtered, newest first (default: last 30 days, max 500 rows)."""
    try:
        end_d = _date.fromisoformat(end) if end else _date.today()
        start_d = _date.fromisoformat(start) if start else end_d - _td(days=29)
    except ValueError:
        raise HTTPException(400, "start/end must be YYYY-MM-DD")
    if start_d > end_d:
        raise HTTPException(400, "start is after end")
    if kind not in finance.TXN_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(finance.TXN_KINDS)}")
    try:
        rows, _ = await run_in_threadpool(bridge_client.txns, start_d.isoformat(), end_d.isoformat())
        ctx = await run_in_threadpool(bridge_client.call, "GET", "/context")
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    names = {c["id"]: c["name"] for c in ctx.get("categories", [])}
    r = finance.transactions(rows, names, q, account, category, kind, min(max(limit, 1), 500))
    return {"from": start_d.isoformat(), "to": end_d.isoformat(), **r}


@app.get("/finance/trends")
async def finance_trends(months: int = 12):
    """Monthly income / spending / categories and month-end balances for the last N months (max 24)."""
    months = min(max(months, 1), 24)
    today = _date.today()
    start = _date(today.year, today.month, 1)
    for _ in range(months - 1):
        start = (start - _td(days=1)).replace(day=1)
    try:
        actual = await run_in_threadpool(bridge_client.call, "GET", f"/finance/summary?months={months}")
        rows, _ = await run_in_threadpool(bridge_client.txns, start.isoformat(), today.isoformat())
    except bridge_client.BridgeError as e:
        raise HTTPException(502, str(e))
    return finance.trends(actual, rows, _date.fromisoformat(actual.get("today") or today.isoformat()))


# ── MCP audit trail (events from mcp / mcp-public) + Telegram digest ─────────
# Not reachable through the UI: nginx refuses /api/audit/ (see frontend/nginx.conf).
import asyncio
import logging

import audit as mcp_audit


@app.post("/audit/mcp")
async def audit_ingest(body: dict):
    await run_in_threadpool(mcp_audit.record, body)
    return {"ok": True}


@app.get("/audit/mcp")
async def audit_summary(hours: float = 24):
    import time as _time
    return await run_in_threadpool(mcp_audit.summarize, _time.time() - hours * 3600)


@app.post("/audit/mcp/digest")
async def audit_digest(force: bool = True, send: bool = True):
    """Send the Telegram digest now (force) — or send=false to preview the text."""
    try:
        r = await run_in_threadpool(mcp_audit.digest, force, send)
    except (httpx.HTTPError, RuntimeError) as e:
        raise HTTPException(502, str(e))
    return {k: v for k, v in r.items() if k != "summary"}


async def _audit_digest_loop():
    while True:
        try:
            await run_in_threadpool(mcp_audit.digest)
        except Exception as e:                      # keep going; the next tick retries
            logging.getLogger("audit").warning("audit digest failed: %s", e)
        await asyncio.sleep(300)


@app.on_event("startup")
async def _start_audit_digest():
    if mcp_audit.telegram_configured():
        asyncio.create_task(_audit_digest_loop())
