<script>
  import "../app.css";
  import ActualSidebar from "../components/ActualSidebar.svelte";
  import CategoryMapper from "../components/CategoryMapper.svelte";
  import RulesAudit from "../components/RulesAudit.svelte";
  import { onMount } from "svelte";
  import HealthStatus from "../components/HealthStatus.svelte";
  import AccountSuggestion from "../components/AccountSuggestion.svelte";
  import ReviewQueue from "../components/ReviewQueue.svelte";
  import ReconcilePanel from "../components/ReconcilePanel.svelte";

  const API = "/api";

  // ── Actual state (lifted from sidebar) ──────────────────────────────────────
  let actualConnected = false;
  let actualBudgetLoaded = false;
  let actualAccounts = [];
  let actualCategoryGroups = [];
  let actualPayees = [];
  let actualRules = [];
  let actualAccountId = "";

  function onActualChange(e) {
    actualConnected = e.detail.connected;
    actualBudgetLoaded = e.detail.budgetLoaded;
    actualAccounts = e.detail.accounts;
    actualCategoryGroups = e.detail.categoryGroups;
    actualPayees = e.detail.payees;
    actualRules = e.detail.rules;
    actualAccountId = e.detail.selectedAccountId;
  }

  // ── Statement state ──────────────────────────────────────────────────────────
  let transactions = [];
  let detectedBank = "";
  let loading = false;
  let parseError = "";
  let dragover = false;

  // ── View ─────────────────────────────────────────────────────────────────────
  let tab = "transactions"; // 'transactions' | 'summary'

  // ── Transaction filters ──────────────────────────────────────────────────────
  let searchQuery = "";
  let filterCategory = "All";
  let sortBy = "date";
  let sortDesc = true;
  let editingId = null;
  let editingCategory = "";
  let showCredits = true;

  // ── Import state ──────────────────────────────────────────────────────────────
  let showMapper = false; // category mapping modal (unmapped seed categories → Actual)
  let showAudit = false; // rules audit modal
  let learnRules = true; // after import, turn manual category edits into Actual payee rules
  let useLLM = true; // ask the LLM about merchants nothing else recognises
  let llmStatus = null; // { enabled, provider, model }
  let catStats = null; // { actual, seed, llm, manual, transfer, review, unmapped }
  let recategorising = false;
  let taxonomy = []; // fallback category list when Actual isn't connected
  let statementInfo = null; // what the statement is: bank, card vs savings, last 4, balance
  let accountRec = null; // recommender result: { suggestions, recommended, auto_select }
  let accountTouched = false; // user picked an account themselves after this upload
  let showReview = false; // review queue modal
  let reviewPending = 0; // items waiting for approval
  async function refreshReviewCount() {
    try { reviewPending = (await (await fetch(`${API}/review/counts`)).json()).pending || 0; } catch {}
  }
  onMount(() => { refreshReviewCount(); const t = setInterval(refreshReviewCount, 60000); return () => clearInterval(t); });
  let importing = false;
  let importResult = null;
  let importError = "";
  let dryRunResult = null;
  let verifications = {}; // { [imported_id]: 'skip' | 'import' } for ambiguous dupes
  let includeCredits = true; // send credit (deposit/refund) rows alongside debits
  let confirmDestination = false; // user must tick "import to this account" before import

  // ── Categories & colours ──────────────────────────────────────────────────────
  const UNCAT = "Uncategorized";
  function catColor(name) {
    if (!name || name === UNCAT) return "#6b7280";
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 65% 55%)`;
  }
  $: actualCats = actualCategoryGroups.flatMap((g) =>
    (g.categories || [])
      .filter((c) => !c.hidden)
      .map((c) => ({ ...c, group: g.name })),
  );
  $: CATEGORIES =
    actualBudgetLoaded && actualCats.length
      ? [...new Set(actualCats.map((c) => c.name)), UNCAT]
      : [...taxonomy, UNCAT];

  const SOURCE_LABEL = {
    actual: "Actual rule",
    seed: "Seed rule",
    llm: "AI guess",
    manual: "Manual",
  };

  onMount(async () => {
    try {
      taxonomy =
        (await (await fetch(`${API}/taxonomy`)).json()).categories || [];
    } catch {}
    try {
      llmStatus = await (await fetch(`${API}/llm/status`)).json();
    } catch {}
  });

  // ── Derived ───────────────────────────────────────────────────────────────────
  $: displayTxns = showCredits
    ? transactions
    : transactions.filter((t) => !t.is_credit);
  $: filtered = displayTxns
    .filter((t) => {
      const ms =
        !searchQuery ||
        t.description.toLowerCase().includes(searchQuery.toLowerCase());
      const mc = filterCategory === "All" || t.category === filterCategory;
      return ms && mc;
    })
    .sort((a, b) => {
      let va = a[sortBy],
        vb = b[sortBy];
      if (sortBy === "date") {
        va = new Date(a.date);
        vb = new Date(b.date);
      }
      if (sortBy === "amount") {
        va = a.amount;
        vb = b.amount;
      }
      return sortDesc ? (vb > va ? 1 : -1) : va > vb ? 1 : -1;
    });

  $: spending = transactions.filter((t) => !t.is_credit);
  $: importable = includeCredits ? transactions : spending;
  $: totalSpend = spending.reduce((s, t) => s + t.amount, 0);
  $: categorisedCount = transactions.filter(
    (t) => t.category !== "Uncategorized",
  ).length;

  $: summaryData = (() => {
    const m = {};
    for (const t of spending) {
      m[t.category] = (m[t.category] || 0) + t.amount;
    }
    return Object.entries(m)
      .map(([cat, total]) => ({ cat, total }))
      .sort((a, b) => b.total - a.total);
  })();

  // ── File upload ───────────────────────────────────────────────────────────────
  async function uploadFile(file) {
    if (!file) return;
    loading = true;
    parseError = "";
    importResult = null;
    dryRunResult = null;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const qs = new URLSearchParams({
        account_id: actualAccountId || "",
        use_llm: String(useLLM),
      });
      const res = await fetch(`${API}/parse?${qs}`, {
        method: "POST",
        body: fd,
      });
      if (!res.ok)
        throw new Error(
          (await res.json().catch(() => ({}))).detail || res.statusText,
        );
      const data = await res.json();
      transactions = data.transactions.map((t, i) => ({ ...t, id: i }));
      catStats = data.stats;
      detectedBank = data.bank || "";
      statementInfo = data.statement || null;
      accountTouched = false;
      applyAccountRec(data.account);
      lastCtxKey = ctxKey;
    } catch (e) {
      parseError = e.message;
    } finally {
      loading = false;
    }
  }

  function onDrop(e) {
    dragover = false;
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  }

  // ── Category editing ──────────────────────────────────────────────────────────
  function startEdit(t) {
    editingId = t.id;
    editingCategory = t.category;
  }
  function saveEdit(t) {
    if (editingId !== t.id) return;
    const picked = editingCategory;
    editingId = null;
    if (picked === t.category) return;
    const hit = actualCats.find((c) => c.name === picked);
    // apply to every row with the same payee — that's what the learned rule will do too
    transactions = transactions.map((tx) =>
      tx.id === t.id ||
      (tx.payee &&
        tx.payee === t.payee &&
        tx.is_credit === t.is_credit &&
        tx.source !== "manual")
        ? {
            ...tx,
            category: picked,
            category_id: hit?.id ?? null,
            source: "manual",
            confidence: 1,
            unmapped: false,
            notes: (tx.notes || "").replace(/#llm|#review/g, "").trim(),
          }
        : tx,
    );
  }

  // ── Re-categorise when the Actual context changes ────────────────────────────
  // (connect/disconnect, switch account, change aliases). Manual edits are kept.
  $: ctxKey = `${actualBudgetLoaded}|${actualAccountId}|${actualRules.length}|${actualCats.length}`;
  let lastCtxKey = "";
  $: if (transactions.length && ctxKey !== lastCtxKey) recategorise();

  async function recategorise() {
    lastCtxKey = ctxKey;
    if (!transactions.length) return;
    recategorising = true;
    try {
      const res = await fetch(`${API}/categorize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          transactions,
          account_id: actualAccountId || null,
          use_llm: useLLM,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        transactions = data.transactions.map((t, i) => ({
          ...t,
          id: transactions[i]?.id ?? i,
        }));
        catStats = data.stats;
      }
    } catch {
    } finally {
      recategorising = false;
    }
  }

  $: unmappedCount = transactions.filter((t) => t.unmapped).length;

  async function refreshCategories() {
    try {
      const res = await fetch(`${API}/actual/categories`);
      if (res.ok)
        actualCategoryGroups =
          (await res.json()).categoryGroups || actualCategoryGroups;
    } catch {}
    try {
      const res = await fetch(`${API}/actual/rules`);
      if (res.ok) actualRules = (await res.json()).rules || actualRules;
    } catch {}
  }

  // ── Import ────────────────────────────────────────────────────────────────────
  // Backend now generates imported_id (ref-based or hash); legacy ids cover older formats.
  function legacyStmtId(t) {
    return `stmt-${t.date}-${t.description}-${t.amount}`.replace(/\s+/g, "-");
  }

  // Old hash format: sha256("date|desc|abs(amount)")[:16] — kept so re-imports of
  // statements ingested before the sign/currency-aware hash still match existing rows.
  async function legacyHash(t) {
    const enc = new TextEncoder().encode(
      `${t.date}|${t.description}|${Math.abs(t.amount)}`,
    );
    const buf = await crypto.subtle.digest("SHA-256", enc);
    return [...new Uint8Array(buf)]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
      .slice(0, 16);
  }

  async function buildPayload(dr = false) {
    const rows = await Promise.all(
      importable.map(async (t) => ({
        ...t,
        category_id: t.category_id || undefined,
        notes: t.notes || "",
        legacy_ids: [
          ...(t.legacy_ids || []),
          legacyStmtId(t),
          await legacyHash(t),
        ],
      })),
    );
    return {
      accountId: actualAccountId,
      dryRun: dr,
      verified: verifications,
      transactions: rows,
    };
  }

  $: unresolvedVerify = (dryRunResult?.toVerify ?? []).filter(
    (t) => !verifications[t.imported_id],
  );
  $: hasUnresolved = unresolvedVerify.length > 0;

  async function runDryRun() {
    if (!actualAccountId) {
      importError = "Select an account in the sidebar first";
      return;
    }
    importing = true;
    importError = "";
    dryRunResult = null;
    verifications = {};
    try {
      const payload = await buildPayload(true);
      const res = await fetch(`${API}/actual/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      dryRunResult = data;
    } catch (e) {
      importError = e.message;
    } finally {
      importing = false;
    }
  }

  async function doImport() {
    if (!actualAccountId) {
      importError = "Select an account in the sidebar first";
      return;
    }
    if (!confirmDestination) {
      importError =
        'Tick "Yes, import to this account" to confirm the destination';
      return;
    }
    importing = true;
    importError = "";
    importResult = null;
    try {
      const payload = await buildPayload(false);
      const res = await fetch(`${API}/actual/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      importResult = data;
      confirmDestination = false;
      await rememberAccount();
      await scanAfterImport();
      if (learnRules) await learnFromManualEdits();
    } catch (e) {
      importError = e.message;
    } finally {
      importing = false;
    }
  }

  // Manual edits become "payee is X → category" rules in Actual, so next month's
  // statement (and the scheduler) gets them right without this UI.
  async function learnFromManualEdits() {
    const seen = new Set();
    const mappings = [];
    for (const t of importable) {
      if (
        t.source !== "manual" ||
        !t.category_id ||
        !t.payee ||
        seen.has(t.payee)
      )
        continue;
      seen.add(t.payee);
      mappings.push({ description: t.payee, categoryId: t.category_id });
    }
    if (!mappings.length) return;
    try {
      const res = await fetch(`${API}/actual/rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mappings }),
      });
      const data = await res.json();
      if (res.ok)
        importResult = { ...importResult, rulesCreated: data.created };
    } catch {}
  }

  // Reset confirmation whenever the destination account changes.
  $: {
    actualAccountId;
    confirmDestination = false;
  }

  $: destinationAccount = actualAccounts.find((a) => a.id === actualAccountId);

  // ── Account recommender ──────────────────────────────────────────────────────
  // Auto-select only when the backend is confident and the user hasn't chosen.
  let applyingRec = false;
  function applyAccountRec(rec) {
    accountRec = rec || null;
    if (rec?.auto_select && rec.recommended && !accountTouched) {
      applyingRec = true;
      actualAccountId = rec.recommended;
      applyingRec = false;
    }
  }
  function pickAccount(id) {
    accountTouched = true;
    actualAccountId = id;
  }
  // a manual change in the sidebar counts as the user's choice
  $: if (actualAccountId && !applyingRec && accountRec && actualAccountId !== accountRec.recommended) accountTouched = true;

  // connected to Actual after uploading → ask for a recommendation now
  let recAskedFor = null; // statement object we already asked about (prevents a fetch loop)
  $: if (actualBudgetLoaded && statementInfo && !accountRec?.suggestions?.length && recAskedFor !== statementInfo) refreshAccountRec();
  let recLoading = false;
  async function refreshAccountRec() {
    if (recLoading) return;
    recAskedFor = statementInfo;
    recLoading = true;
    try {
      const res = await fetch(`${API}/accounts/recommend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statement: statementInfo, transactions }),
      });
      if (res.ok) applyAccountRec(await res.json());
    } catch {}
    finally { recLoading = false; }
  }

  // after an import: look for duplicates / unlinked transfers in the statement period
  let scanNote = "";
  async function scanAfterImport() {
    const dates = importable.map((t) => t.date).sort();
    if (!dates.length) return;
    try {
      const res = await fetch(`${API}/review/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start: dates[0], end: dates[dates.length - 1] }),
      });
      const d = await res.json();
      if (res.ok) scanNote = d.queued || d.auto_applied
        ? `${d.queued} item(s) to review${d.auto_applied ? `, ${d.auto_applied} transfer(s) linked from what you taught it` : ""}`
        : "";
    } catch {}
    refreshReviewCount();
  }

  // after a successful import, remember "this card/account number → this Actual account"
  async function rememberAccount() {
    if (!statementInfo?.fingerprint || !actualAccountId) return;
    try {
      await fetch(`${API}/accounts/remember`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          fingerprint: statementInfo.fingerprint,
          account_id: actualAccountId,
          account_name: destinationAccount?.name || "",
        }),
      });
      if (accountRec) accountRec = { ...accountRec, remembered: actualAccountId };
    } catch {}
  }
  $: newCountEstimate = importable.length;
  function fmtAccountBalance(b) {
    if (b === null || b === undefined) return "";
    return (b / 100).toLocaleString("en-SG", {
      style: "currency",
      currency: "SGD",
    });
  }

  // ── CSV export ────────────────────────────────────────────────────────────────
  async function exportCSV() {
    const res = await fetch(`${API}/export/csv`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ transactions: filtered }),
    });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "budget_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  // ── Formatters ────────────────────────────────────────────────────────────────
  function fmtAmt(n) {
    return n.toLocaleString("en-SG", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }
  function fmtDate(d) {
    return new Date(d).toLocaleDateString("en-SG", {
      day: "2-digit",
      month: "short",
      year: "numeric",
    });
  }
  function srcClass(t) {
    return t.source || (t.kind === "transfer" ? "transfer" : "none");
  }
  function srcLabel(t) {
    if (t.source)
      return (
        SOURCE_LABEL[t.source] +
        (t.source === "llm" ? ` ${Math.round(t.confidence * 100)}%` : "")
      );
    if (t.kind === "transfer") return "Transfer";
    if (t.kind === "p2p") return "PayNow/FAST";
    return "—";
  }
</script>

{#if showReview}
  <ReviewQueue
    on:close={() => { showReview = false; refreshReviewCount(); }}
    on:changed={refreshReviewCount}
    on:counts={(e) => (reviewPending = e.detail.pending || 0)}
  />
{/if}

<!-- Category Mapping Modal: seed categories with no match in Actual → pick one (saved as aliases) -->
{#if showMapper}
  <CategoryMapper
    transactions={transactions.filter((t) => t.unmapped)}
    {actualCategoryGroups}
    on:save={async () => {
      showMapper = false;
      await refreshCategories();
      lastCtxKey = "";
    }}
    on:close={() => (showMapper = false)}
  />
{/if}

{#if showAudit}
  <RulesAudit
    sampleDescriptions={transactions.map((t) => t.description)}
    on:applied={async () => {
      await refreshCategories();
      lastCtxKey = "";
    }}
    on:close={() => (showAudit = false)}
  />
{/if}

<div class="app">
  <!-- ── SIDEBAR ── -->
  <ActualSidebar
    bind:connected={actualConnected}
    bind:budgetLoaded={actualBudgetLoaded}
    bind:accounts={actualAccounts}
    bind:categoryGroups={actualCategoryGroups}
    bind:payees={actualPayees}
    bind:rules={actualRules}
    bind:selectedAccountId={actualAccountId}
    on:change={onActualChange}
  />

  <!-- ── MAIN ── -->
  <div class="main">
    <!-- Top bar -->
    <header class="topbar">
      <div class="topbar-left">
        <span class="app-title">💳 Budget Parser</span>
        {#if detectedBank}
          <span class="badge" title={statementInfo?.evidence?.join(" · ") ?? ""}
            >{statementInfo?.label ?? detectedBank}</span
          >
        {/if}
        <a class="ghost icon-btn money-link" href="/money">💰 Money</a>
        {#if actualBudgetLoaded}
          <button class="ghost icon-btn" class:has-pending={reviewPending} on:click={() => (showReview = true)}
            title="Possible duplicates, unlinked transfers and reconciliation fixes waiting for you"
            >🧾 Review{reviewPending ? ` (${reviewPending})` : ""}</button>
          <button
            class="ghost icon-btn"
            on:click={() => (showAudit = true)}
            title="Audit & sync Actual rules">🧹 Rules audit</button
          >
        {/if}
        {#if llmStatus?.enabled}
          <label
            class="checkbox-inline"
            title="{llmStatus.provider} · {llmStatus.model}"
          >
            <input
              type="checkbox"
              bind:checked={useLLM}
              on:change={() => {
                lastCtxKey = "";
              }}
            /> AI fallback
          </label>
        {/if}
      </div>
      {#if transactions.length}
        <div class="topbar-tabs">
          <button
            class="tab-btn"
            class:active={tab === "transactions"}
            on:click={() => (tab = "transactions")}
          >
            Transactions <span class="badge muted">{transactions.length}</span>
          </button>
          <button
            class="tab-btn"
            class:active={tab === "summary"}
            on:click={() => (tab = "summary")}
          >
            Summary
          </button>
        </div>
        <div class="topbar-right">
          <button class="ghost icon-btn" on:click={exportCSV} title="Export CSV"
            >⬇ CSV</button
          >
          <button
            class="ghost icon-btn"
            on:click={() => {
              transactions = [];
              detectedBank = "";
              statementInfo = null;
              accountRec = null;
              importResult = null;
              dryRunResult = null;
            }}
          >
            ✕ Clear
          </button>
        </div>
      {/if}
          <HealthStatus />
    </header>

    <div class="content">
      <!-- ── EMPTY STATE / DROP ZONE ── -->
      {#if !transactions.length}
        <div class="upload-area">
          <!-- svelte-ignore a11y-no-static-element-interactions -->
          <div
            class="dropzone"
            class:dragover
            on:dragover|preventDefault={() => (dragover = true)}
            on:dragleave={() => (dragover = false)}
            on:drop|preventDefault={onDrop}
            on:click={() => document.getElementById("fi").click()}
          >
            {#if loading}
              <span class="spinner lg"></span>
              <p>Parsing statement…</p>
            {:else}
              <div class="drop-icon">📂</div>
              <p class="drop-title">Drop your bank statement here</p>
              <p class="drop-sub">or click to browse</p>
              <div class="banks">
                <span>UOB</span><span>DBS / POSB</span><span>OCBC</span><span
                  >XLS / XLSX / CSV / PDF</span
                >
              </div>
            {/if}
          </div>
          <input
            id="fi"
            type="file"
            accept=".xls,.xlsx,.csv,.pdf"
            style="display:none"
            on:change={(e) => uploadFile(e.target.files[0])}
          />
          {#if parseError}
            <div class="error-msg">{parseError}</div>
          {/if}
        </div>

        <!-- ── TRANSACTIONS ── -->
      {:else if tab === "transactions"}
        <!-- Import bar -->
        {#if actualBudgetLoaded}
          <div class="import-bar">
            <div class="import-bar-left">
              {#if importResult}
                <span class="success-msg">
                  ✓ Imported: {importResult.added} added, {importResult.updated}
                  updated{importResult.skipped
                    ? `, ${importResult.skipped} skipped`
                    : ""}{importResult.rulesCreated
                    ? ` · ${importResult.rulesCreated} rules learned`
                    : ""}
                </span>
              {:else if dryRunResult}
                <span class="dryrun-msg">
                  🔍 Dry run: {dryRunResult.added} new, {dryRunResult.skipped} already
                  imported{dryRunResult.toVerify?.length
                    ? `, ${dryRunResult.toVerify.length} need review`
                    : ""}
                </span>
              {:else if importError}
                <span class="error-msg">{importError}</span>
              {:else}
                <span class="import-hint">
                  {actualAccountId
                    ? `Ready to import ${importable.length} transactions`
                    : "← Select account in sidebar"}
                </span>
              {/if}
            </div>
            <div class="import-bar-right">
              <label
                class="checkbox-inline"
                title="Include deposit/refund rows alongside debits"
              >
                <input type="checkbox" bind:checked={includeCredits} />
                Credits
              </label>
              {#if unmappedCount}
                <button
                  class="ghost icon-btn warn"
                  on:click={() => (showMapper = true)}
                  title="Seed categories with no matching category in Actual"
                >
                  🗂 Map {unmappedCount} unmapped
                </button>
              {/if}
              <label
                class="checkbox-inline"
                title="Turn your manual category edits into Actual payee rules after import"
              >
                <input type="checkbox" bind:checked={learnRules} /> Learn rules
              </label>
              <button
                class="ghost icon-btn"
                on:click={runDryRun}
                disabled={importing || !actualAccountId}
              >
                {importing ? "…" : "🔍 Dry Run"}
              </button>
              <button
                class="primary icon-btn"
                on:click={doImport}
                disabled={importing ||
                  !actualAccountId ||
                  hasUnresolved ||
                  !confirmDestination}
              >
                {importing
                  ? "…"
                  : hasUnresolved
                    ? `⚠ Review ${unresolvedVerify.length} first`
                    : "⬆ Import to Actual"}
              </button>
            </div>
          </div>

          {#if importResult && actualAccountId && statementInfo}
            <ReconcilePanel
              accountId={actualAccountId}
              accountName={destinationAccount?.name ?? ""}
              statement={statementInfo}
              {transactions}
              on:queued={refreshReviewCount}
              on:openReview={() => (showReview = true)}
            />
            {#if scanNote}<div class="scan-note">🧾 {scanNote} — <button class="linkish" on:click={() => (showReview = true)}>review</button></div>{/if}
          {/if}

          <AccountSuggestion
            statement={statementInfo}
            rec={accountRec}
            selectedId={actualAccountId}
            total={transactions.length}
            on:pick={(e) => pickAccount(e.detail)}
          />

          {#if actualAccountId && destinationAccount}
            <div class="confirm-card">
              <div class="confirm-row">
                <span class="confirm-label">Destination</span>
                <strong>{destinationAccount.name}</strong>
                {#if destinationAccount.balance !== null && destinationAccount.balance !== undefined}
                  <span class="confirm-balance"
                    >· {fmtAccountBalance(destinationAccount.balance)}</span
                  >
                {/if}
              </div>
              <div class="confirm-row counts">
                <span
                  ><strong>{dryRunResult?.added ?? newCountEstimate}</strong> new</span
                >
                <span
                  ><strong>{dryRunResult?.skipped ?? "—"}</strong> duplicate</span
                >
                <span
                  ><strong>{dryRunResult?.toVerify?.length ?? 0}</strong> need review</span
                >
              </div>
              <label class="confirm-check">
                <input type="checkbox" bind:checked={confirmDestination} />
                Yes, import to <strong>{destinationAccount.name}</strong>
              </label>
            </div>
          {/if}
        {/if}

        <!-- Duplicate verification panel -->
        {#if dryRunResult?.toVerify?.length > 0}
          <div class="verify-panel">
            <div class="verify-header">
              <span
                >⚠ {dryRunResult.toVerify.length} possible duplicate{dryRunResult
                  .toVerify.length > 1
                  ? "s"
                  : ""} — choose skip or import for each</span
              >
              <div class="verify-bulk">
                <button
                  class="ghost icon-btn"
                  on:click={() => {
                    verifications = Object.fromEntries(
                      dryRunResult.toVerify.map((t) => [t.imported_id, "skip"]),
                    );
                    verifications = verifications;
                  }}>Skip all</button
                >
                <button
                  class="ghost icon-btn"
                  on:click={() => {
                    verifications = Object.fromEntries(
                      dryRunResult.toVerify.map((t) => [
                        t.imported_id,
                        "import",
                      ]),
                    );
                    verifications = verifications;
                  }}>Import all</button
                >
              </div>
            </div>
            {#each dryRunResult.toVerify as t}
              <div
                class="verify-row"
                class:resolved={!!verifications[t.imported_id]}
              >
                <span class="verify-date">{t.date}</span>
                <span class="verify-desc">{t.description}{#if t.reason}<small class="verify-reason"> — {t.reason}</small>{/if}</span>
                <span class="verify-amt"
                  >{t.currency} {t.amount.toFixed(2)}</span
                >
                <div class="verify-actions">
                  <button
                    class="verify-btn"
                    class:active={verifications[t.imported_id] === "skip"}
                    on:click={() => {
                      verifications[t.imported_id] = "skip";
                      verifications = verifications;
                    }}>Skip</button
                  >
                  <button
                    class="verify-btn import"
                    class:active={verifications[t.imported_id] === "import"}
                    on:click={() => {
                      verifications[t.imported_id] = "import";
                      verifications = verifications;
                    }}>Import</button
                  >
                </div>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Filters -->
        <div class="toolbar">
          <input
            class="search-input"
            placeholder="🔍 Search…"
            bind:value={searchQuery}
          />
          <select bind:value={filterCategory}>
            <option value="All">All Categories</option>
            {#each CATEGORIES as c}<option value={c}>{c}</option>{/each}
          </select>
          <select bind:value={sortBy}>
            <option value="date">Date</option>
            <option value="amount">Amount</option>
            <option value="category">Category</option>
          </select>
          <button class="ghost icon-btn" on:click={() => (sortDesc = !sortDesc)}
            >{sortDesc ? "↓" : "↑"}</button
          >
          <label class="row-label">
            <input type="checkbox" bind:checked={showCredits} /> Credits
          </label>
        </div>

        <!-- Stats -->
        <div class="stats-row">
          <div class="stat-chip">
            <span>Showing</span><strong>{filtered.length}</strong>
          </div>
          <div class="stat-chip">
            <span>Total Spend</span><strong>SGD {fmtAmt(totalSpend)}</strong>
          </div>
          <div class="stat-chip">
            <span>Auto-categorised</span><strong
              >{categorisedCount}/{transactions.length}</strong
            >
          </div>
          {#if catStats}
            <div
              class="stat-chip"
              title="Actual rules · seed rules · AI · manual"
            >
              <span>By</span><strong
                >{catStats.actual} rule · {catStats.seed} seed · {catStats.llm} AI</strong
              >
            </div>
            {#if catStats.review}<div class="stat-chip warn">
                <span>#review</span><strong>{catStats.review}</strong>
              </div>{/if}
          {/if}
          {#if recategorising}<span class="spinner"></span>{/if}
        </div>

        <!-- Table -->
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Category</th>
                <th>Source</th>
                <th class="r">Amount</th>
              </tr>
            </thead>
            <tbody>
              {#each filtered as t (t.id)}
                <tr class:credit={t.is_credit}>
                  <td class="td-date">{fmtDate(t.date)}</td>
                  <td class="td-desc">
                    <span class="payee">{t.payee || t.description}</span>
                    {#if t.payee && t.payee !== t.description}<span
                        class="raw-desc">{t.description}</span
                      >{/if}
                    {#if t.foreign_amount}
                      <span class="foreign"
                        >{t.foreign_currency} {t.foreign_amount}</span
                      >
                    {/if}
                  </td>
                  <td>
                    {#if editingId === t.id}
                      <select
                        bind:value={editingCategory}
                        on:change={() => saveEdit(t)}
                        on:blur={() => saveEdit(t)}
                      >
                        {#if actualBudgetLoaded && actualCats.length}
                          {#each actualCategoryGroups as g}
                            <optgroup label={g.name}>
                              {#each (g.categories || []).filter((c) => !c.hidden) as c}<option
                                  value={c.name}>{c.name}</option
                                >{/each}
                            </optgroup>
                          {/each}
                          <option value={UNCAT}>{UNCAT}</option>
                        {:else}
                          {#each CATEGORIES as c}<option value={c}>{c}</option
                            >{/each}
                        {/if}
                      </select>
                    {:else}
                      <button
                        class="cat-badge"
                        class:unmapped={t.unmapped}
                        style="--c:{catColor(t.category)}"
                        title={t.unmapped
                          ? "No matching category in Actual — use “Map unmapped”"
                          : ""}
                        on:click={() => startEdit(t)}
                      >
                        {t.category}{t.unmapped ? " ⚠" : ""} ✎
                      </button>
                    {/if}
                  </td>
                  <td>
                    <span class="src {srcClass(t)}">{srcLabel(t)}</span>
                  </td>
                  <td class="r amt" class:credit-amt={t.is_credit}>
                    {t.is_credit ? "+" : ""}{t.currency}
                    {fmtAmt(t.amount)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        <!-- ── SUMMARY ── -->
      {:else if tab === "summary"}
        <div class="summary">
          <div class="summary-header">
            <h2>Spending Summary</h2>
            <p>
              SGD {fmtAmt(totalSpend)} across {spending.length} transactions
            </p>
          </div>
          <div class="summary-grid">
            {#each summaryData as { cat, total }}
              <div class="summary-card">
                <div class="sc-top">
                  <span class="sc-dot" style="background:{catColor(cat)}"
                  ></span>
                  <span class="sc-name">{cat}</span>
                  <span class="sc-pct"
                    >{((total / totalSpend) * 100).toFixed(1)}%</span
                  >
                </div>
                <div class="sc-bar-bg">
                  <div
                    class="sc-bar"
                    style="width:{(total / totalSpend) *
                      100}%;background:{catColor(cat)}"
                  ></div>
                </div>
                <div class="sc-amt">SGD {fmtAmt(total)}</div>
              </div>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .app {
    display: flex;
    height: 100vh;
    overflow: hidden;
  }
  .main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
  }

  /* Top bar */
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 20px;
    height: 52px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .topbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
  }
  .app-title {
    font-size: 16px;
    font-weight: 700;
  }
  .topbar-tabs {
    display: flex;
    gap: 2px;
  }
  .tab-btn {
    background: transparent;
    border: none;
    color: var(--text2);
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
  }
  .tab-btn:hover,
  .tab-btn.active {
    background: var(--surface2);
    color: var(--text);
  }
  .tab-btn.active {
    color: var(--accent);
  }
  .topbar-right {
    display: flex;
    gap: 8px;
  }

  /* Content area */
  .content {
    flex: 1;
    overflow-y: auto;
    padding: 0;
  }

  /* Upload */
  .upload-area {
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: center;
    justify-content: center;
    height: 100%;
    padding: 40px;
  }
  .dropzone {
    width: 100%;
    max-width: 480px;
    border: 2px dashed var(--border);
    border-radius: 16px;
    padding: 60px 40px;
    cursor: pointer;
    text-align: center;
    transition: all 0.2s;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    background: var(--surface);
  }
  .dropzone.dragover {
    border-color: var(--accent);
    background: var(--surface2);
  }
  .drop-icon {
    font-size: 48px;
  }
  .drop-title {
    font-size: 18px;
    font-weight: 600;
  }
  .drop-sub {
    color: var(--text2);
    font-size: 13px;
  }
  .banks {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: center;
    margin-top: 8px;
  }
  .banks span {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 12px;
    font-size: 12px;
    color: var(--text2);
  }

  /* Import bar */
  .import-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding: 10px 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .import-bar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
    min-width: 0;
  }
  .import-bar-right {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
  }
  .import-hint {
    font-size: 13px;
    color: var(--text2);
  }
  .dryrun-msg {
    font-size: 13px;
    color: var(--accent);
    background: #6c63ff11;
    border: 1px solid #6c63ff33;
    border-radius: 6px;
    padding: 5px 10px;
  }
  .checkbox-inline {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: var(--text2);
    user-select: none;
  }
  .checkbox-inline input {
    margin: 0;
  }

  /* Destination confirmation card */
  .confirm-card {
    margin: 10px 20px 0;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .confirm-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: var(--text2);
    flex-wrap: wrap;
  }
  .confirm-row strong {
    color: var(--text);
  }
  .confirm-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text2);
  }
  .confirm-balance {
    color: var(--text2);
  }
  .confirm-row.counts {
    gap: 16px;
  }
  .confirm-check {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    cursor: pointer;
    user-select: none;
    padding-top: 4px;
    border-top: 1px dashed var(--border);
  }
  .confirm-check input {
    margin: 0;
  }

  /* Verification panel */
  .verify-panel {
    border-bottom: 1px solid var(--border);
    background: #ff980011;
  }
  .verify-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 20px;
    font-size: 13px;
    color: #b45309;
    font-weight: 500;
    background: #ff980018;
    border-bottom: 1px solid #ff980033;
  }
  .verify-bulk {
    display: flex;
    gap: 6px;
  }
  .verify-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 20px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  .verify-row.resolved {
    opacity: 0.6;
  }
  .verify-date {
    color: var(--text2);
    min-width: 90px;
    flex-shrink: 0;
  }
  .verify-desc {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .verify-amt {
    min-width: 90px;
    text-align: right;
    font-weight: 500;
    flex-shrink: 0;
  }
  .verify-actions {
    display: flex;
    gap: 4px;
    flex-shrink: 0;
  }
  .verify-btn {
    padding: 3px 10px;
    border-radius: 5px;
    font-size: 12px;
    font-weight: 500;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text2);
    cursor: pointer;
  }
  .verify-btn.active {
    background: var(--surface2);
    color: var(--text);
    border-color: var(--text2);
  }
  .verify-btn.import.active {
    background: #6c63ff22;
    color: var(--accent);
    border-color: var(--accent);
  }

  /* Toolbar */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
  }
  .search-input {
    flex: 1;
    min-width: 160px;
  }

  /* Stats */
  .stats-row {
    display: flex;
    gap: 10px;
    padding: 10px 20px;
    flex-wrap: wrap;
  }
  .stat-chip {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 14px;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .stat-chip span {
    font-size: 11px;
    color: var(--text2);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .stat-chip strong {
    font-size: 16px;
    font-weight: 700;
  }

  /* Table */
  .table-wrap {
    overflow-x: auto;
    padding: 0 20px 20px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  thead {
    position: sticky;
    top: 0;
    background: var(--surface2);
    z-index: 1;
  }
  th {
    padding: 10px 12px;
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text2);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
    font-size: 13px;
  }
  tr:hover td {
    background: var(--surface2);
  }
  tr.credit td {
    opacity: 0.5;
  }
  .r {
    text-align: right;
  }

  .td-date {
    white-space: nowrap;
    color: var(--text2);
  }
  .td-desc {
    max-width: 260px;
  }
  .td-desc span {
    display: block;
  }
  .foreign {
    color: var(--text2);
    font-size: 12px;
  }

  .cat-badge {
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    border: 1px solid;
    background: transparent;
    white-space: nowrap;
    cursor: pointer;
  }
  .cat-badge:hover {
    opacity: 0.8;
  }

  .src {
    font-size: 11px;
    padding: 2px 7px;
    border-radius: 999px;
    white-space: nowrap;
    background: var(--surface2);
    color: var(--text2);
  }
  .src.actual {
    background: #00c9a722;
    color: var(--accent2);
  }
  .src.seed {
    background: #3b82f622;
    color: #60a5fa;
  }
  .src.llm {
    background: #f7931e22;
    color: var(--warn);
  }
  .src.manual {
    background: #a855f722;
    color: #c084fc;
  }
  .payee {
    display: block;
    font-weight: 500;
  }
  .raw-desc {
    display: block;
    font-size: 11px;
    color: var(--text2);
  }
  .cat-badge {
    background: color-mix(in srgb, var(--c) 14%, transparent);
    color: var(--c);
    border-color: color-mix(in srgb, var(--c) 35%, transparent);
  }
  .cat-badge.unmapped {
    outline: 1px dashed var(--warn);
  }
  .stat-chip.warn strong {
    color: var(--warn);
  }
  button.warn {
    color: var(--warn);
  }
  button.has-pending { color: var(--warn); }
  .money-link { text-decoration: none; color: var(--text2); display: inline-flex; align-items: center; }
  .topbar :global(.icon-btn) { white-space: nowrap; }
  .topbar-left .badge { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .scan-note { margin: 6px 20px 0; font-size: 12px; color: var(--text2); }
  .linkish { background: none; border: none; color: var(--accent); padding: 0; cursor: pointer; font-size: 12px; }

  .amt {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .credit-amt {
    color: var(--accent2);
  }

  /* Summary */
  .summary {
    padding: 24px 20px;
  }
  .summary-header {
    margin-bottom: 20px;
  }
  .summary-header h2 {
    font-size: 20px;
    font-weight: 700;
  }
  .summary-header p {
    color: var(--text2);
    font-size: 13px;
    margin-top: 4px;
  }
  .summary-grid {
    display: grid;
    gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  }
  .summary-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px;
  }
  .sc-top {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .sc-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .sc-name {
    flex: 1;
    font-weight: 500;
    font-size: 13px;
  }
  .sc-pct {
    font-size: 12px;
    color: var(--text2);
  }
  .sc-bar-bg {
    height: 5px;
    background: var(--surface2);
    border-radius: 999px;
    overflow: hidden;
    margin-bottom: 8px;
  }
  .sc-bar {
    height: 100%;
    border-radius: 999px;
  }
  .sc-amt {
    font-size: 18px;
    font-weight: 700;
  }
</style>
