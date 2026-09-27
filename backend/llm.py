"""
Optional LLM fallback for transactions that neither your Actual rules nor the seed
merchant table recognise.

Design:
  * OFF by default. Enable with LLM_PROVIDER=ollama | openai | anthropic.
  * Only the cleaned payee name, the raw description and the direction
    (spend/refund) are sent — never account numbers, balances or your name.
    For full privacy point it at Ollama on your own box.
  * The model must pick from YOUR category list (Actual's, or the taxonomy when
    not connected). Anything outside the list is discarded.
  * One call per batch of unique payees, results cached in DATA_DIR/llm_cache.json
    so each merchant is only ever asked about once.
  * Confidence is capped at LLM_MAX_CONFIDENCE and the import tags these rows
    with #llm in Actual notes, so you can filter and review them.

Env:
  LLM_PROVIDER        none | ollama | openai | anthropic         (default none)
  LLM_MODEL           e.g. qwen2.5:7b / llama3.1:8b (ollama), gpt-4o-mini, claude-haiku-4-5
  LLM_BASE_URL        OpenAI-compatible base URL. Default for ollama:
                      http://ollama:11434/v1 ; for openai: https://api.openai.com/v1
                      (works with LM Studio, vLLM, OpenRouter, LiteLLM …)
  LLM_API_KEY         not needed for ollama
  LLM_MIN_CONFIDENCE  below this the suggestion is dropped            (default 0.5)
  LLM_MAX_CONFIDENCE  cap shown in the UI                              (default 0.75)
  LLM_BATCH_SIZE      payees per request                               (default 40)
  LLM_TIMEOUT         seconds                                          (default 90)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("llm")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
CACHE_FILE = DATA_DIR / "llm_cache.json"

DEFAULT_MODELS = {
    "ollama": "qwen2.5:7b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}
DEFAULT_BASE = {
    "ollama": "http://ollama:11434/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}

SYSTEM_PROMPT = (
    "You categorise personal bank and credit-card transactions from Singapore for a "
    "household budget. You are given a fixed list of allowed categories and a list of "
    "transactions. For each transaction choose exactly one allowed category, or null if "
    "you genuinely cannot tell (e.g. a person's name, a generic transfer). Use your "
    "knowledge of Singapore merchants, hawker stalls, chains and abbreviations. "
    "Respond with JSON only, no prose."
)


class LLMCategorizer:
    def __init__(self, provider: str | None = None, model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, cache_file: Path | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "none")).strip().lower()
        self.model = model or os.getenv("LLM_MODEL") or DEFAULT_MODELS.get(self.provider, "")
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or DEFAULT_BASE.get(self.provider, "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.min_conf = float(os.getenv("LLM_MIN_CONFIDENCE", "0.5"))
        self.max_conf = float(os.getenv("LLM_MAX_CONFIDENCE", "0.75"))
        self.batch_size = int(os.getenv("LLM_BATCH_SIZE", "40"))
        self.timeout = float(os.getenv("LLM_TIMEOUT", "90"))
        self.transport = transport
        self.cache_file = cache_file or CACHE_FILE
        self._cache = None

    # ── status ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        if self.provider in ("ollama",):
            return bool(self.model and self.base_url)
        if self.provider in ("openai", "anthropic"):
            return bool(self.model and self.api_key)
        return False

    def status(self) -> dict:
        return {"enabled": self.enabled, "provider": self.provider, "model": self.model,
                "base_url": self.base_url if self.provider != "anthropic" else None,
                "cached_payees": len(self.cache)}

    # ── health ────────────────────────────────────────────────────────────────
    def ping(self, timeout: float = 20.0) -> dict:
        """Tiny real request to prove the endpoint, key and model all work."""
        base = {"provider": self.provider, "model": self.model}
        if not self.enabled:
            reason = ("LLM_PROVIDER is none" if self.provider in ("", "none")
                      else "missing LLM_MODEL / LLM_API_KEY / LLM_BASE_URL")
            return {**base, "status": "disabled", "detail": reason}
        prompt = 'Health check. Reply with exactly this JSON and nothing else: {"ok": true}'
        saved, self.timeout = self.timeout, min(self.timeout, timeout)
        t0 = time.monotonic()
        try:
            text = self._anthropic(prompt) if self.provider == "anthropic" else self._openai(prompt)
            ms = round((time.monotonic() - t0) * 1000)
            data = _parse_json(text)
            if isinstance(data, dict):
                return {**base, "status": "ok", "latency_ms": ms}
            return {**base, "status": "degraded", "latency_ms": ms,
                    "detail": f"model replied but not with JSON: {str(text)[:120]!r}"}
        except httpx.HTTPStatusError as e:
            body = e.response.text[:200].replace("\n", " ")
            hint = {401: "bad API key", 403: "key not allowed / region", 404: "wrong model or base URL",
                    429: "rate limited / quota"}.get(e.response.status_code, "")
            return {**base, "status": "error", "latency_ms": round((time.monotonic() - t0) * 1000),
                    "detail": f"HTTP {e.response.status_code}{' (' + hint + ')' if hint else ''}: {body}"}
        except Exception as e:
            return {**base, "status": "error", "latency_ms": round((time.monotonic() - t0) * 1000),
                    "detail": f"{type(e).__name__}: {e}"[:240]}
        finally:
            self.timeout = saved

    # ── cache ─────────────────────────────────────────────────────────────────
    @property
    def cache(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_file.read_text())
            except Exception:
                self._cache = {}
        return self._cache

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self.cache, indent=1, sort_keys=True))
        except Exception as e:  # cache is best-effort
            log.warning("could not write LLM cache: %s", e)

    @staticmethod
    def cache_key(payee: str, is_credit: bool) -> str:
        return f"{(payee or '').strip().lower()}|{'cr' if is_credit else 'dr'}"

    # ── main entry ────────────────────────────────────────────────────────────
    def categorize(self, items: list[dict], allowed: list[str]) -> dict[str, dict]:
        """
        items: [{key, payee, description, is_credit}] — key must be unique.
        returns {key: {"category": name, "confidence": float, "cached": bool}}
        """
        if not self.enabled or not items or not allowed:
            return {}
        allowed_lc = {a.lower(): a for a in allowed}
        results: dict[str, dict] = {}
        todo = []
        for it in items:
            ck = self.cache_key(it["payee"], it.get("is_credit", False))
            hit = self.cache.get(ck)
            if hit and (hit.get("category") or "").lower() in allowed_lc:
                results[it["key"]] = {"category": allowed_lc[hit["category"].lower()],
                                      "confidence": hit.get("confidence", self.max_conf), "cached": True}
            elif hit and hit.get("category") is None and hit.get("allowed_hash") == _hash(allowed):
                continue  # model already said "can't tell" for this exact category list
            else:
                todo.append(it)

        for i in range(0, len(todo), self.batch_size):
            batch = todo[i:i + self.batch_size]
            try:
                answers = self._ask(batch, allowed)
            except Exception as e:
                log.warning("LLM request failed: %s", e)
                break
            for it in batch:
                ans = answers.get(it["key"])
                ck = self.cache_key(it["payee"], it.get("is_credit", False))
                cat = (ans or {}).get("category")
                conf = _clamp((ans or {}).get("confidence", 0.6))
                if cat and str(cat).lower() in allowed_lc:
                    cat = allowed_lc[str(cat).lower()]
                    conf = min(conf, self.max_conf)
                    self.cache[ck] = {"category": cat, "confidence": conf, "model": self.model,
                                      "ts": int(time.time())}
                    if conf >= self.min_conf:
                        results[it["key"]] = {"category": cat, "confidence": conf, "cached": False}
                else:
                    self.cache[ck] = {"category": None, "allowed_hash": _hash(allowed),
                                      "model": self.model, "ts": int(time.time())}
        self._save_cache()
        return results

    # ── transport ─────────────────────────────────────────────────────────────
    def _prompt(self, batch: list[dict], allowed: list[str]) -> str:
        rows = [{"id": str(n), "payee": it["payee"], "raw": it.get("description", ""),
                 "direction": "money in (refund/income)" if it.get("is_credit") else "spend"}
                for n, it in enumerate(batch)]
        return (
            "Allowed categories:\n" + json.dumps(allowed, ensure_ascii=False) +
            "\n\nTransactions:\n" + json.dumps(rows, ensure_ascii=False) +
            '\n\nReturn: {"results": [{"id": "<id>", "category": "<one allowed category or null>", '
            '"confidence": <0..1>}]}'
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def _ask(self, batch: list[dict], allowed: list[str]) -> dict[str, dict]:
        prompt = self._prompt(batch, allowed)
        text = self._anthropic(prompt) if self.provider == "anthropic" else self._openai(prompt)
        data = _parse_json(text)
        by_idx = {}
        for r in (data or {}).get("results", []):
            try:
                by_idx[int(r.get("id"))] = r
            except (TypeError, ValueError):
                continue
        return {it["key"]: by_idx[n] for n, it in enumerate(batch) if n in by_idx}

    def _openai(self, prompt: str, system: str = SYSTEM_PROMPT) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        with self._client() as c:
            r = c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            if r.status_code == 400:  # some servers reject response_format
                body.pop("response_format")
                r = c.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    def _anthropic(self, prompt: str, system: str = SYSTEM_PROMPT) -> str:
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        body = {"model": self.model, "max_tokens": 4096, "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        with self._client() as c:
            r = c.post(f"{self.base_url}/v1/messages", json=body, headers=headers)
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", []))


def _clamp(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _hash(allowed: list[str]) -> str:
    import hashlib
    return hashlib.sha1("|".join(sorted(allowed)).encode()).hexdigest()[:10]


def _parse_json(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)  # tolerate ```json fences / chatter
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


# ── generic helpers used by the review / reconciliation agents ────────────────

def complete_json(llm: "LLMCategorizer", system: str, prompt: str):
    """One JSON-mode completion with a custom system prompt → parsed dict (or None)."""
    text = llm._anthropic(prompt, system) if llm.provider == "anthropic" else llm._openai(prompt, system)
    return _parse_json(text)


def run_tools(llm: "LLMCategorizer", system: str, user: str, tools: list[dict],
              handlers: dict, max_steps: int = 8) -> dict:
    """
    Minimal tool-calling loop for OpenAI-compatible APIs (OpenAI, Gemini, Ollama, …)
    and Anthropic. tools: [{"name", "description", "parameters": <JSON schema>}];
    handlers: name → callable(**args) returning something JSON-serialisable.
    Returns {"text": final answer, "calls": [{"tool", "args", "result"}]}.
    """
    calls: list[dict] = []

    def call(name, args):
        fn = handlers.get(name)
        try:
            result = fn(**(args or {})) if fn else {"error": f"unknown tool {name}"}
        except Exception as e:  # tools report errors back to the model instead of crashing the loop
            result = {"error": f"{type(e).__name__}: {e}"}
        calls.append({"tool": name, "args": args, "result": result})
        return json.dumps(result, default=str)[:20000]

    with llm._client() as c:
        if llm.provider == "anthropic":
            headers = {"x-api-key": llm.api_key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            msgs = [{"role": "user", "content": user}]
            atools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                      for t in tools]
            for _ in range(max_steps):
                r = c.post(f"{llm.base_url}/v1/messages", headers=headers, json={
                    "model": llm.model, "max_tokens": 4096, "temperature": 0, "system": system,
                    "tools": atools, "messages": msgs})
                r.raise_for_status()
                content = r.json().get("content", [])
                uses = [b for b in content if b.get("type") == "tool_use"]
                if not uses:
                    return {"text": "".join(b.get("text", "") for b in content), "calls": calls}
                msgs.append({"role": "assistant", "content": content})
                msgs.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": u["id"], "content": call(u["name"], u.get("input"))}
                    for u in uses]})
        else:
            headers = {"Content-Type": "application/json"}
            if llm.api_key:
                headers["Authorization"] = f"Bearer {llm.api_key}"
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            otools = [{"type": "function", "function": t} for t in tools]
            for _ in range(max_steps):
                r = c.post(f"{llm.base_url}/chat/completions", headers=headers, json={
                    "model": llm.model, "temperature": 0, "messages": msgs, "tools": otools})
                r.raise_for_status()
                msg = r.json()["choices"][0]["message"]
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    return {"text": msg.get("content") or "", "calls": calls}
                msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tcs})
                for tc in tcs:
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": call(tc["function"]["name"], args)})
    return {"text": "(stopped after max tool steps)", "calls": calls}
