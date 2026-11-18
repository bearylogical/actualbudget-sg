<!--
  App shell: the top nav switches what fills the page (Import · Review · Rules ·
  History · Money), addressed by the URL hash so tabs are linkable.

  Import flow: Statement → Categories → Import → Reconcile.
  One step on screen at a time, one primary action per step; the destination
  account is chosen per statement (never carried over from a previous import).
  It stays mounted while another tab is showing, so switching away mid-import
  keeps the statement.
-->
<script>
  import "../app.css";
  import { onMount } from "svelte";
  import ActualSidebar from "../components/ActualSidebar.svelte";
  import CategoryMapper from "../components/CategoryMapper.svelte";
  import RulesAudit from "../components/RulesAudit.svelte";
  import HealthStatus from "../components/HealthStatus.svelte";
  import ReviewQueue from "../components/ReviewQueue.svelte";
  import HistoryView from "../components/HistoryView.svelte";
  import MoneyView from "../components/MoneyView.svelte";
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
  let sessionId = "";       // the Import session on the server (POST /imports)
  let guard = null;         // wrong-account guard for the chosen account (blocks step 1)
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

  // ── Tabs ──────────────────────────────────────────────────────────────────────
  const TABS = [
    { id: "import", label: "Import" },
    { id: "review", label: "Review", needsActual: true, title: "Possible duplicates, unlinked transfers and reconciliation fixes waiting for you" },
    { id: "rules", label: "Rules", needsActual: true, title: "Audit and sync your Actual rules" },
    { id: "history", label: "History", title: "Every statement uploaded here or dropped in the watch folder" },
    { id: "money", label: "Money" },
  ];
  let view = "import";
  function readHash() {
    const h = location.hash.slice(1);
    view = TABS.some((t) => t.id === h) ? h : "import";
  }
  $: tab = TABS.find((t) => t.id === view);
  $: if (view === "review") refreshReviewCount();

  let showMapper = false;
  let reviewPending = 0;
  let showConn = false;

  async function refreshReviewCount() {
    try { reviewPending = (await (await fetch(`${API}/review/counts`)).json()).pending || 0; } catch {}
  }
  onMount(() => {
    readHash();
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
  // The server holds the statement (an Import session); this page only sends your decisions.
  async function uploadFile(file) {
    loading = true;
    parseError = "";
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${API}/imports?use_llm=${useLLM && aiAvailable}`, { method: "POST", body: fd });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const data = await res.json();
      resetImport();
      transactions = [];
      accountId = "";
      sessionId = data.session;
      seenBefore = data.seen_before || [];
      applyPreview(data);
      // pre-select only when the server is confident (remembered, or a clear recommendation)
      if (data.account?.selected) accountId = data.account.selected;
      lastCtxKey = ctxKey;
      step = 1; reached = 1;
    } catch (e) {
      parseError = e.message;
    } finally {
      loading = false;
    }
  }

  function resetImport() {
    importResult = null; rulesCreated = 0; scanNote = "";
  }
  function startOver() {
    transactions = []; statementInfo = null; sessionId = ""; seenBefore = []; accountId = ""; accountRec = null;
    catStats = null; guard = null;
    resetImport(); step = 1; reached = 1;
  }

  function applyPreview(data) {
    const ack = new Map(transactions.map((t) => [t.id, t.ack]));
    transactions = data.transactions.map((t) => ({ ...t, id: t.imported_id, ack: ack.get(t.imported_id) }));
    catStats = data.stats;
    statementInfo = data.statement || null;
    accountRec = data.account || null;
    guard = data.guard || null;
  }

  // ── Your decisions (sent with every preview and with the commit) ─────────────
  function categoryDecisions(rows) {
    const out = {};
    for (const t of rows) {
      if (t.source === "manual") out[t.imported_id] = { category: t.category, category_id: t.category_id || null };
      if (t.confirmed) out[t.imported_id] = { ...(out[t.imported_id] || {}), confirmed: true };
    }
    return out;
  }
  $: excluded = includeCredits ? [] : transactions.filter((t) => t.is_credit).map((t) => t.imported_id);
  $: commitBody = { account_id: accountId, categories: categoryDecisions(transactions), exclude: excluded,
                    use_llm: useLLM && aiAvailable, learn_rules: learnRules, reconcile_use_llm: false };

  function pickAccount(id) {
    if (id === accountId || importResult) return; // undo the import first to move it
    accountId = id;
    reached = Math.min(reached, 2); // later steps depend on the account
  }

  // ── Re-preview when the context changes (account, rules, categories) ─────────
  $: ctxKey = `${actualBudgetLoaded}|${accountId}|${actualRules.length}|${actualCats.length}|${useLLM}`;
  let lastCtxKey = "";
  $: if (sessionId && !importResult && ctxKey !== lastCtxKey) recategorise();
  $: recLoading = recategorising;
  let changedIds = new Set();   // rows whose category changed on the last Refresh
  let refreshNote = "";
  async function recategorise(refresh = false) {
    lastCtxKey = ctxKey;
    if (!sessionId) return;
    recategorising = true;
    const before = new Map(transactions.map((t) => [t.id, t.category_id || t.category]));
    try {
      const res = await fetch(`${API}/imports/${sessionId}/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...commitBody, account_id: accountId || null, refresh }),
      });
      const data = await res.json();
      if (res.ok) {
        if (refresh) {
          changedIds = new Set(data.transactions.filter((t) => before.get(t.imported_id) !== (t.category_id || t.category))
            .map((t) => t.imported_id));
          refreshNote = `${changedIds.size} ${changedIds.size === 1 ? "category" : "categories"} changed` +
            (data.history_size ? ` · learned from ${data.history_size} of your past transactions` : "");
        }
        applyPreview(data);
        if (!accountId && data.account?.selected) accountId = data.account.selected;
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

  // ── After import: the server already remembered the account, scanned and reconciled ──
  function afterImport() {
    reached = Math.max(reached, 4);
    const st = importResult?.steps || {};
    rulesCreated = st.learn_rules?.created || 0;
    const scan = st.scan || {};
    const held = st.hold_duplicates?.held || 0;
    const queued = (scan.queued || 0) + held;
    scanNote = queued || scan.auto_applied
      ? `${queued} item(s) to review${scan.auto_applied ? `, ${scan.auto_applied} linked or removed automatically` : ""}`
      : "";
    refreshReviewCount();
  }
  function afterUndo() {
    rulesCreated = 0; scanNote = "";
    reached = 3;
    lastCtxKey = "";   // preview again
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
    { n: 3, label: "Import", sub: importResult ? `${importResult.import.added} imported` : reached >= 3 ? "Not yet imported" : "" },
    { n: 4, label: "Reconcile", sub: reached >= 4 ? "Check balance" : "After import" },
  ];
  $: status = importResult ? "Imported" : transactions.length ? "Not imported" : "—";
  const goReview = () => (location.hash = "review");
  function fmtDay(d) { return d ? new Date(d).toLocaleDateString("en-SG", { day: "numeric", month: "short" }) : ""; }
</script>

<svelte:window on:hashchange={readHash} />
<svelte:head><title>{tab.id === "import" ? "Budget Parser" : `${tab.label} · Budget Parser`}</title></svelte:head>

{#if showMapper}
  <CategoryMapper
    transactions={transactions.filter((t) => t.unmapped)}
    {actualCategoryGroups}
    on:save={async () => { showMapper = false; await refreshCategories(); lastCtxKey = ""; }}
    on:close={() => (showMapper = false)}
  />
{/if}

<div class="app">
  <header class="topbar">
    <div class="brand"><span class="brand-icon"><Icon name="card" size={22} /></span>Budget Parser</div>
    <nav aria-label="Main">
      {#each TABS as t}
        <a class="tab" class:on={view === t.id} href="#{t.id}" title={t.title}
          aria-current={view === t.id ? "page" : undefined}>
          {t.label}{#if t.id === "review" && reviewPending}<span class="count">{reviewPending}</span>{/if}
        </a>
      {/each}
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

  <!-- connection card: shown on tabs that need Actual until a budget is loaded; stays mounted to keep the session -->
  <div class="connect" class:hidden={actualBudgetLoaded || !(view === "import" || tab.needsActual)}>
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
    <div class="body" class:hidden={view !== "import"}>
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
            {guard}
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
            {sessionId}
            {commitBody}
            {rulesCreated}
            {scanNote}
            bind:result={importResult}
            on:imported={afterImport}
            on:undone={afterUndo}
            on:openReview={goReview}
            on:back={() => (step = 2)}
            on:continue={() => go(4)}
            on:new={startOver}
          />
        {:else if step === 4}
          <StepReconcile
            {accountId}
            {accountName}
            statement={statementInfo}
            count={importResult?.sent_ids?.length ?? 0}
            initial={importResult?.reconcile}
            {sessionId}
            {aiAvailable}
            on:queued={refreshReviewCount}
            on:openReview={goReview}
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

  {#if view !== "import" && (actualBudgetLoaded || !tab.needsActual)}
    <main class="page">
      <div class="page-inner">
        {#if view === "review"}
          <ReviewQueue
            on:changed={refreshReviewCount}
            on:counts={(e) => (reviewPending = e.detail.pending || 0)}
          />
        {:else if view === "rules"}
          <RulesAudit
            sampleDescriptions={transactions.map((t) => t.description)}
            on:applied={async () => { await refreshCategories(); lastCtxKey = ""; }}
          />
        {:else if view === "history"}
          <HistoryView />
        {:else if view === "money"}
          <MoneyView />
        {/if}
      </div>
    </main>
  {/if}
</div>

<style>
  .app { display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

  .topbar { height: 64px; flex-shrink: 0; display: flex; align-items: center; gap: 24px; padding: 0 32px; border-bottom: 1px solid var(--border-soft); }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 17px; white-space: nowrap; }
  .brand-icon { color: var(--accent); display: inline-flex; }
  nav { display: flex; align-items: center; margin-left: 16px; }
  .tab { background: none; border: 0; border-radius: 0; color: var(--text2); font-size: 14px; font-weight: 500; padding: 0 14px; height: 64px; border-bottom: 2px solid transparent; text-decoration: none; display: inline-flex; align-items: center; gap: 8px; }
  .tab:hover { color: var(--text); }
  .tab.on { color: var(--text); border-bottom-color: var(--accent); }
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
  .body.hidden { display: none; }

  .page { flex: 1; min-height: 0; overflow-y: auto; padding: 32px; }
  .page-inner { max-width: 1200px; margin: 0 auto; }
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
    .page { padding: 16px; }
  }
</style>
