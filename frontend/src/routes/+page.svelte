<!--
  Import flow: Statement → Categories → Import → Reconcile.
  One step on screen at a time, one primary action per step; the destination
  account is chosen per statement (never carried over from a previous import).
-->
<script>
  import "../app.css";
  import { onMount } from "svelte";
  import ActualSidebar from "../components/ActualSidebar.svelte";
  import CategoryMapper from "../components/CategoryMapper.svelte";
  import RulesAudit from "../components/RulesAudit.svelte";
  import HealthStatus from "../components/HealthStatus.svelte";
  import ReviewQueue from "../components/ReviewQueue.svelte";
  import Icon from "../components/Icon.svelte";
  import StepStatement from "../components/import/StepStatement.svelte";
  import StepCategories from "../components/import/StepCategories.svelte";
  import StepImport from "../components/import/StepImport.svelte";
  import StepReconcile from "../components/import/StepReconcile.svelte";

  const API = "/api";
  const UNCAT = "Uncategorized";

  // ── Actual connection (owned by ActualSidebar, mirrored here) ────────────────
  let actual; // component instance, for disconnect()
  let actualConnected = false;
  let actualBudgetLoaded = false;
  let actualAccounts = [];
  let actualCategoryGroups = [];
  let actualPayees = [];
  let actualRules = [];
  function onActualChange(e) {
    actualConnected = e.detail.connected;
    actualBudgetLoaded = e.detail.budgetLoaded;
    actualAccounts = e.detail.accounts;
    actualCategoryGroups = e.detail.categoryGroups;
    actualPayees = e.detail.payees;
    actualRules = e.detail.rules;
  }

  // ── Flow ──────────────────────────────────────────────────────────────────────
  let step = 1;
  let reached = 1;
  function go(n) { step = n; reached = Math.max(reached, n); }

  // ── Statement ─────────────────────────────────────────────────────────────────
  let transactions = [];
  let statementInfo = null;
  let uploadId = "";        // import-history record for this file (from /parse)
  let seenBefore = [];      // earlier imports of the exact same file
  let loading = false;
  let parseError = "";
  let catStats = null;
  let recategorising = false;
  let taxonomy = [];

  // ── Account ───────────────────────────────────────────────────────────────────
  let accountId = "";
  let accountRec = null;
  let recLoading = false;
  let stepOneBlocked = true;

  // ── Options ───────────────────────────────────────────────────────────────────
  let includeCredits = true;
  let learnRules = true;
  let useLLM = true;
  let llmStatus = null;

  // ── Import / after ────────────────────────────────────────────────────────────
  let importResult = null;
  let rulesCreated = 0;
  let scanNote = "";
  let undoneAccountId = ""; // after an undo, re-importing here may restore the deleted rows

  // ── Modals ────────────────────────────────────────────────────────────────────
  let showMapper = false;
  let showAudit = false;
  let showReview = false;
  let reviewPending = 0;
  let showConn = false;

  async function refreshReviewCount() {
    try { reviewPending = (await (await fetch(`${API}/review/counts`)).json()).pending || 0; } catch {}
  }
  onMount(() => {
    refreshReviewCount();
    const t = setInterval(refreshReviewCount, 60000);
    (async () => {
      try { taxonomy = (await (await fetch(`${API}/taxonomy`)).json()).categories || []; } catch {}
      try { llmStatus = await (await fetch(`${API}/llm/status`)).json(); } catch {}
    })();
    return () => clearInterval(t);
  });

  // ── Derived ───────────────────────────────────────────────────────────────────
  $: actualCats = actualCategoryGroups.flatMap((g) =>
    (g.categories || []).filter((c) => !c.hidden).map((c) => ({ ...c, group: g.name })));
  $: CATEGORIES = actualBudgetLoaded && actualCats.length
    ? [...new Set(actualCats.map((c) => c.name)), UNCAT]
    : [...taxonomy, UNCAT];
  $: importable = includeCredits ? transactions : transactions.filter((t) => !t.is_credit);
  $: creditCount = transactions.filter((t) => t.is_credit).length;
  $: uncategorisedCount = importable.filter((t) => t.kind !== "transfer" && (!t.category || t.category === UNCAT || t.unmapped)).length;
  $: needsCount = importable.filter((t) => !t.ack && (t.unmapped || (t.kind !== "transfer" && (!t.category || t.category === UNCAT)) || (t.source === "llm" && (t.confidence ?? 0) < 0.8))).length;
  $: account = actualAccounts.find((a) => a.id === accountId);
  $: accountName = account?.name ?? "";
  $: aiAvailable = !!llmStatus?.enabled;

  // ── Upload ────────────────────────────────────────────────────────────────────
  async function uploadFile(file) {
    loading = true;
    parseError = "";
    try {
      const qs = new URLSearchParams({ account_id: "", use_llm: String(useLLM && aiAvailable) });
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${API}/parse?${qs}`, { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const data = await res.json();
      resetImport();
      transactions = data.transactions.map((t, i) => ({ ...t, id: i }));
      catStats = data.stats;
      statementInfo = data.statement || null;
      uploadId = data.upload_id || "";
      seenBefore = data.seen_before || [];
      accountId = "";
      accountRec = null;
      recAskedFor = null;
      applyAccountRec(data.account);
      lastCtxKey = ctxKey;
      step = 1; reached = 1;
    } catch (e) {
      parseError = e.message;
    } finally {
      loading = false;
    }
  }

  function resetImport() {
    importResult = null; rulesCreated = 0; scanNote = ""; undoneAccountId = "";
  }
  function startOver() {
    transactions = []; statementInfo = null; uploadId = ""; seenBefore = []; accountId = ""; accountRec = null; catStats = null;
    resetImport(); step = 1; reached = 1;
  }

  // ── Account recommendation ────────────────────────────────────────────────────
  function applyAccountRec(rec) {
    accountRec = rec || null;
    // pre-select only when the backend is confident; otherwise the user picks
    if (rec?.auto_select && rec.recommended && !accountId) accountId = rec.recommended;
  }
  let recAskedFor = null;
  $: if (actualBudgetLoaded && statementInfo && !accountRec?.suggestions?.length && recAskedFor !== statementInfo) refreshAccountRec();
  async function refreshAccountRec() {
    if (recLoading) return;
    recAskedFor = statementInfo;
    recLoading = true;
    try {
      const res = await fetch(`${API}/accounts/recommend`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statement: statementInfo, transactions }),
      });
      if (res.ok) applyAccountRec(await res.json());
    } catch {} finally { recLoading = false; }
  }
  function pickAccount(id) {
    if (id === accountId || importResult) return; // undo the import first to move it
    accountId = id;
    reached = Math.min(reached, 2); // later steps depend on the account
  }

  // ── Re-categorise when the context changes (account, rules, categories) ──────
  $: ctxKey = `${actualBudgetLoaded}|${accountId}|${actualRules.length}|${actualCats.length}|${useLLM}`;
  let lastCtxKey = "";
  $: if (transactions.length && ctxKey !== lastCtxKey) recategorise();
  let changedIds = new Set();   // rows whose category changed on the last Refresh
  let refreshNote = "";
  async function recategorise(refresh = false) {
    lastCtxKey = ctxKey;
    if (!transactions.length) return;
    recategorising = true;
    const before = new Map(transactions.map((t) => [t.id, t.category_id || t.category]));
    try {
      const res = await fetch(`${API}/categorize`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transactions, account_id: accountId || null, use_llm: useLLM && aiAvailable, refresh }),
      });
      const data = await res.json();
      if (res.ok && refresh) {
        changedIds = new Set(data.transactions
          .map((t, i) => [transactions[i]?.id ?? i, t.category_id || t.category])
          .filter(([id, c]) => before.get(id) !== c).map(([id]) => id));
        refreshNote = `${changedIds.size} ${changedIds.size === 1 ? "category" : "categories"} changed` +
          (data.history_size ? ` · learned from ${data.history_size} of your past transactions` : "");
      }
      if (res.ok) {
        transactions = data.transactions.map((t, i) => ({ ...t, id: transactions[i]?.id ?? i, ack: transactions[i]?.ack,
          // an accepted guess stays accepted only if the category didn't change underneath it
          confirmed: transactions[i]?.confirmed && transactions[i]?.category === t.category }));
        catStats = data.stats;
      }
    } catch {} finally { recategorising = false; }
  }

  // Refresh: pick up rules, mappings and history you've added since this statement was read.
  async function refreshAll() {
    await refreshCategories();
    await recategorise(true);
  }

  async function refreshCategories() {
    try {
      const res = await fetch(`${API}/actual/categories`);
      if (res.ok) actualCategoryGroups = (await res.json()).categoryGroups || actualCategoryGroups;
    } catch {}
    try {
      const res = await fetch(`${API}/actual/rules`);
      if (res.ok) actualRules = (await res.json()).rules || actualRules;
    } catch {}
  }

  // ── Import payload ────────────────────────────────────────────────────────────
  // Legacy ids let re-imports of statements ingested before the current hash still match.
  function legacyStmtId(t) {
    return `stmt-${t.date}-${t.description}-${t.amount}`.replace(/\s+/g, "-");
  }
  async function legacyHash(t) {
    const enc = new TextEncoder().encode(`${t.date}|${t.description}|${Math.abs(t.amount)}`);
    const buf = await crypto.subtle.digest("SHA-256", enc);
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
  }
  async function buildPayload(dryRun, verified) {
    const rows = await Promise.all(importable.map(async ({ ack, confirmed, ...t }) => ({
      ...t,
      category_id: t.category_id || undefined,
      // an accepted AI guess is your choice now: drop #llm so it counts as your history
      notes: (confirmed ? (t.notes || "").replace(/#llm/g, "").trim() : t.notes) || "",
      legacy_ids: [...(t.legacy_ids || []), legacyStmtId(t), await legacyHash(t)],
    })));
    const payload = { accountId, dryRun, verified, transactions: rows, uploadId, accountName };
    if (undoneAccountId && undoneAccountId === accountId) payload.reimportDeleted = true;
    return payload;
  }

  // ── After import ──────────────────────────────────────────────────────────────
  async function afterImport() {
    reached = Math.max(reached, 4);
    await rememberAccount();
    await scanAfterImport();
    if (learnRules) await learnFromManualEdits();
  }
  async function rememberAccount() {
    if (!statementInfo?.fingerprint || !accountId) return;
    try {
      await fetch(`${API}/accounts/remember`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fingerprint: statementInfo.fingerprint, account_id: accountId, account_name: accountName }),
      });
    } catch {}
  }
  async function scanAfterImport() {
    const dates = importable.map((t) => t.date).sort();
    if (!dates.length) return;
    try {
      const res = await fetch(`${API}/review/scan`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start: dates[0], end: dates[dates.length - 1], uploadId }),
      });
      const d = await res.json();
      if (res.ok) scanNote = d.queued || d.auto_applied
        ? `${d.queued} item(s) to review${d.auto_applied ? `, ${d.auto_applied} transfer(s) linked automatically` : ""}`
        : "";
    } catch {}
    refreshReviewCount();
  }
  // Manual edits and accepted AI guesses become "payee is X → category" rules in Actual.
  async function learnFromManualEdits() {
    const seen = new Set();
    const mappings = [];
    for (const t of importable) {
      if (!(t.source === "manual" || t.confirmed) || !t.category_id || !t.payee || seen.has(t.payee)) continue;
      seen.add(t.payee);
      mappings.push({ description: t.payee, categoryId: t.category_id });
    }
    if (!mappings.length) return;
    try {
      const res = await fetch(`${API}/actual/rules`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mappings }),
      });
      const data = await res.json();
      if (res.ok) rulesCreated = data.created || 0;
    } catch {}
  }
  function afterUndo() {
    undoneAccountId = accountId;
    rulesCreated = 0; scanNote = "";
    reached = 3;
  }

  // ── CSV ───────────────────────────────────────────────────────────────────────
  async function exportCSV(rows) {
    const res = await fetch(`${API}/export/csv`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transactions: rows }),
    });
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = "budget_export.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  // ── Stepper ───────────────────────────────────────────────────────────────────
  $: steps = [
    { n: 1, label: "Statement", sub: !statementInfo ? "Upload a file" : stepOneBlocked ? (accountId ? "Check account" : "Choose account") : accountName },
    { n: 2, label: "Categories", sub: reached < 2 ? "" : needsCount ? `${needsCount} need a look` : "All set" },
    { n: 3, label: "Import", sub: importResult ? `${importResult.added} imported` : reached >= 3 ? "Not yet imported" : "" },
    { n: 4, label: "Reconcile", sub: reached >= 4 ? "Check balance" : "After import" },
  ];
  $: status = importResult ? "Imported" : transactions.length ? "Not imported" : "—";
  function fmtDay(d) { return d ? new Date(d).toLocaleDateString("en-SG", { day: "numeric", month: "short" }) : ""; }
</script>

{#if showReview}
  <ReviewQueue
    on:close={() => { showReview = false; refreshReviewCount(); }}
    on:changed={refreshReviewCount}
    on:counts={(e) => (reviewPending = e.detail.pending || 0)}
  />
{/if}
{#if showMapper}
  <CategoryMapper
    transactions={transactions.filter((t) => t.unmapped)}
    {actualCategoryGroups}
    on:save={async () => { showMapper = false; await refreshCategories(); lastCtxKey = ""; }}
    on:close={() => (showMapper = false)}
  />
{/if}
{#if showAudit}
  <RulesAudit
    sampleDescriptions={transactions.map((t) => t.description)}
    on:applied={async () => { await refreshCategories(); lastCtxKey = ""; }}
    on:close={() => (showAudit = false)}
  />
{/if}

<div class="app">
  <header class="topbar">
    <div class="brand"><span class="brand-icon"><Icon name="card" size={22} /></span>Budget Parser</div>
    <nav aria-label="Main">
      <button class="tab on" aria-current="page">Import</button>
      <button class="tab" disabled={!actualBudgetLoaded} on:click={() => (showReview = true)}
        title="Possible duplicates, unlinked transfers and reconciliation fixes waiting for you">
        Review{#if reviewPending}<span class="count">{reviewPending}</span>{/if}
      </button>
      <button class="tab" disabled={!actualBudgetLoaded} on:click={() => (showAudit = true)} title="Audit and sync your Actual rules">Rules</button>
      <a class="tab" href="/history" title="Every statement uploaded here or dropped in the watch folder">History</a>
      <a class="tab" href="/money">Money</a>
    </nav>
    <div class="top-r">
      {#if actualBudgetLoaded}
        <div class="conn">
          <button class="conn-btn" on:click={() => (showConn = !showConn)} aria-expanded={showConn}>
            <span class="dot"></span> Connected to Actual
          </button>
          {#if showConn}
            <div class="conn-pop">
              <div class="muted">{actualAccounts.filter((a) => !a.closed).length} open accounts</div>
              <button class="ghost sm" on:click={() => { showConn = false; actual.disconnect(); startOver(); }}>Disconnect</button>
            </div>
          {/if}
        </div>
      {/if}
      <HealthStatus />
    </div>
  </header>

  <!-- connection card: visible until a budget is loaded; stays mounted to keep the session -->
  <div class="connect" class:hidden={actualBudgetLoaded}>
    <ActualSidebar
      bind:this={actual}
      bind:connected={actualConnected}
      bind:budgetLoaded={actualBudgetLoaded}
      bind:accounts={actualAccounts}
      bind:categoryGroups={actualCategoryGroups}
      bind:payees={actualPayees}
      bind:rules={actualRules}
      on:change={onActualChange}
    />
  </div>

  {#if actualBudgetLoaded}
    <div class="body">
      <aside class="stepper" aria-label="Import steps">
        <div class="eyebrow" style="padding:0 14px 10px">New import</div>
        {#each steps as s}
          {@const done = s.n < step && s.n < reached || (s.n === 3 && importResult && step !== 3)}
          <button class="step" class:current={s.n === step} class:done={done && s.n !== step} class:todo={s.n > reached}
            disabled={s.n > reached || (!transactions.length && s.n > 1)} on:click={() => (step = s.n)}
            aria-current={s.n === step ? "step" : undefined}>
            <span class="sdot">{#if done && s.n !== step}<Icon name="check" size={14} stroke={3} />{:else}{s.n}{/if}</span>
            <span class="stext"><span class="slabel">{s.label}</span>{#if s.sub}<span class="ssub">{s.sub}</span>{/if}</span>
          </button>
        {/each}
      </aside>

      <main class="panel stage">
        {#if step === 1}
          <StepStatement
            statement={statementInfo}
            rec={accountRec}
            accounts={actualAccounts}
            selectedId={accountId}
            total={transactions.length}
            {loading}
            {parseError}
            {recLoading}
            locked={!!importResult}
            {seenBefore}
            bind:blocked={stepOneBlocked}
            on:upload={(e) => uploadFile(e.detail)}
            on:pick={(e) => pickAccount(e.detail)}
            on:continue={() => go(2)}
          />
        {:else if step === 2}
          <StepCategories
            bind:transactions
            categories={CATEGORIES}
            {actualCategoryGroups}
            {actualCats}
            {actualBudgetLoaded}
            {recategorising}
            {catStats}
            {includeCredits}
            {changedIds}
            {refreshNote}
            on:refresh={refreshAll}
            on:openMapper={() => (showMapper = true)}
            on:export={(e) => exportCSV(e.detail)}
            on:back={() => (step = 1)}
            on:continue={() => go(3)}
          />
        {:else if step === 3}
          <StepImport
            {accountId}
            {accountName}
            count={importable.length}
            uncategorised={uncategorisedCount}
            {includeCredits}
            credits={creditCount}
            {buildPayload}
            {uploadId}
            {rulesCreated}
            {scanNote}
            bind:result={importResult}
            on:imported={afterImport}
            on:undone={afterUndo}
            on:openReview={() => (showReview = true)}
            on:back={() => (step = 2)}
            on:continue={() => go(4)}
            on:new={startOver}
          />
        {:else if step === 4}
          <StepReconcile
            {accountId}
            {accountName}
            statement={statementInfo}
            {transactions}
            {uploadId}
            {aiAvailable}
            on:queued={refreshReviewCount}
            on:openReview={() => (showReview = true)}
            on:back={() => (step = 3)}
            on:new={startOver}
          />
        {/if}
      </main>

      <aside class="side">
        <div class="panel card">
          <div class="eyebrow">This import</div>
          <dl>
            <dt>Statement</dt><dd>{statementInfo?.label ?? "—"}</dd>
            {#if statementInfo?.period_start}<dt>Period</dt><dd>{fmtDay(statementInfo.period_start)} – {fmtDay(statementInfo.period_end)}</dd>{/if}
            <dt>Account</dt><dd class:warn-t={statementInfo && stepOneBlocked && accountId} class="strong">{accountName || "—"}</dd>
            <dt>Transactions</dt><dd class="mono">{transactions.length ? importable.length : "—"}</dd>
            <dt>Need a look</dt><dd class="mono" class:warn-t={needsCount}>{transactions.length ? needsCount : "—"}</dd>
            <dt>Status</dt><dd>{status}</dd>
          </dl>
        </div>

        <div class="panel card">
          <div class="eyebrow">Options</div>
          <div class="opt">
            <div><div class="o-name">Include credits</div><div class="o-sub">Deposits, refunds, interest</div></div>
            <button class="switch" class:on={includeCredits} aria-pressed={includeCredits} aria-label="Include credits"
              disabled={!!importResult} on:click={() => (includeCredits = !includeCredits)}></button>
          </div>
          <div class="opt">
            <div><div class="o-name">Remember my categories</div><div class="o-sub">Save your picks and accepted AI guesses as Actual rules</div></div>
            <button class="switch" class:on={learnRules} aria-pressed={learnRules} aria-label="Remember my categories"
              on:click={() => (learnRules = !learnRules)}></button>
          </div>
          {#if aiAvailable}
            <div class="opt">
              <div><div class="o-name">AI suggestions</div><div class="o-sub" title="{llmStatus.provider} · {llmStatus.model}">When no rule matches</div></div>
              <button class="switch" class:on={useLLM} aria-pressed={useLLM} aria-label="AI suggestions"
                on:click={() => (useLLM = !useLLM)}></button>
            </div>
          {/if}
        </div>
      </aside>
    </div>
  {/if}
</div>

<style>
  .app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  .topbar { height: 64px; flex-shrink: 0; display: flex; align-items: center; gap: 24px; padding: 0 32px; border-bottom: 1px solid var(--border-soft); }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 17px; white-space: nowrap; }
  .brand-icon { color: var(--accent); display: inline-flex; }
  nav { display: flex; align-items: center; margin-left: 16px; }
  .tab { background: none; border: 0; border-radius: 0; color: var(--text2); font-size: 14px; font-weight: 500; padding: 0 14px; height: 64px; border-bottom: 2px solid transparent; text-decoration: none; display: inline-flex; align-items: center; gap: 8px; }
  .tab:hover:not(:disabled) { color: var(--text); }
  .tab.on { color: var(--text); border-bottom-color: var(--accent); }
  .tab:disabled { opacity: .45; cursor: not-allowed; }
  .count { background: var(--warn-bg); color: var(--warn); border-radius: 10px; font-size: 12px; padding: 0 7px; font-weight: 600; }
  .top-r { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  .conn { position: relative; }
  .conn-btn { background: none; color: var(--text2); font-size: 13px; padding: 6px 8px; }
  .conn-btn:hover { color: var(--text); }
  .dot { width: 8px; height: 8px; border-radius: 4px; background: var(--accent2); }
  .conn-pop { position: absolute; right: 0; top: 40px; z-index: 20; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 10px; min-width: 200px; font-size: 13px; }

  .connect { flex: 1; display: flex; align-items: flex-start; justify-content: center; padding: 64px 16px; overflow-y: auto; }
  .connect.hidden { display: none; }

  .body { flex: 1; min-height: 0; display: flex; gap: 32px; padding: 32px; }
  .stepper { width: 240px; flex-shrink: 0; display: flex; flex-direction: column; gap: 4px; }
  .step { justify-content: flex-start; align-items: flex-start; gap: 14px; padding: 12px 14px; border-radius: 10px; background: none; text-align: left; width: 100%; color: var(--text); }
  .step:hover:not(:disabled) { background: var(--surface); }
  .step.current { background: var(--surface2); }
  .step.todo, .step:disabled { opacity: .55; cursor: default; }
  .sdot { width: 28px; height: 28px; border-radius: 14px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; border: 1.5px solid #3a404d; color: var(--text2); }
  .step.current .sdot { border-color: var(--accent); color: var(--text); background: #26244a; }
  .step.done .sdot { border-color: var(--accent2); background: var(--ok-bg); color: var(--accent2); }
  .stext { display: flex; flex-direction: column; gap: 2px; padding-top: 3px; min-width: 0; }
  .slabel { font-size: 15px; font-weight: 600; }
  .ssub { font-size: 13px; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .stage { flex: 1; min-width: 0; padding: 32px 36px; display: flex; flex-direction: column; gap: 22px; overflow-y: auto; }

  .side { width: 300px; flex-shrink: 0; display: flex; flex-direction: column; gap: 20px; overflow-y: auto; }
  .card { padding: 22px; display: flex; flex-direction: column; gap: 16px; }
  dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px 12px; font-size: 14px; }
  dt { color: var(--text2); }
  dd { text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  dd.strong { font-weight: 600; }
  .warn-t { color: var(--warn); }
  .opt { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; }
  .o-name { font-size: 14px; font-weight: 500; }
  .o-sub { font-size: 13px; color: var(--text2); }

  @media (max-width: 1100px) {
    .body { flex-direction: column; overflow-y: auto; padding: 16px; gap: 16px; }
    .stepper { width: auto; flex-direction: row; overflow-x: auto; }
    .step { width: auto; }
    .side { width: auto; }
    .stage { padding: 20px; overflow: visible; }
  }
</style>
