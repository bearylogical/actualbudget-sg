<script>
  import '../app.css';
  import ActualSidebar from '../components/ActualSidebar.svelte';
  import CategoryMapper from '../components/CategoryMapper.svelte';

  const API = '/api';

  // ── Actual state (lifted from sidebar) ──────────────────────────────────────
  let actualConnected = false;
  let actualBudgetLoaded = false;
  let actualAccounts = [];
  let actualCategoryGroups = [];
  let actualPayees = [];
  let actualRules = [];
  let actualAccountId = '';

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
  let detectedBank = '';
  let loading = false;
  let parseError = '';
  let dragover = false;

  // ── View ─────────────────────────────────────────────────────────────────────
  let tab = 'transactions'; // 'transactions' | 'summary'

  // ── Transaction filters ──────────────────────────────────────────────────────
  let searchQuery = '';
  let filterCategory = 'All';
  let sortBy = 'date';
  let sortDesc = true;
  let editingId = null;
  let editingCategory = '';
  let showCredits = true;

  // ── Import state ──────────────────────────────────────────────────────────────
  let showMapper = false;       // category mapping modal
  let categoryMap = {};         // ourLabel → actualCategoryId
  let importing = false;
  let importResult = null;
  let importError = '';
  let dryRunResult = null;
  let verifications = {};       // { [imported_id]: 'skip' | 'import' } for ambiguous dupes
  let includeCredits = true;    // send credit (deposit/refund) rows alongside debits
  let confirmDestination = false; // user must tick "import to this account" before import

  // ── Category colours ─────────────────────────────────────────────────────────
  const CAT_COLORS = {
    'Food & Dining': '#f7931e', 'Groceries': '#4caf50', 'Transport': '#2196f3',
    'Shopping': '#e91e63', 'Electronics': '#9c27b0', 'Bills & Utilities': '#ff5722',
    'Software & Cloud': '#00bcd4', 'Subscriptions': '#673ab7', 'Health & Fitness': '#8bc34a',
    'Health & Medical': '#f44336', 'Entertainment': '#ff9800', 'Travel': '#03a9f4',
    'Payment / Transfer': '#607d8b', 'Insurance': '#795548', 'Work / Corporate': '#546e7a',
    'Donations': '#009688', 'Education': '#3f51b5', 'Personal Care': '#e91e63',
    'Uncategorized': '#455a64'
  };
  const CATEGORIES = Object.keys(CAT_COLORS);

  // ── Derived ───────────────────────────────────────────────────────────────────
  $: displayTxns = showCredits ? transactions : transactions.filter(t => !t.is_credit);
  $: filtered = displayTxns
    .filter(t => {
      const ms = !searchQuery || t.description.toLowerCase().includes(searchQuery.toLowerCase());
      const mc = filterCategory === 'All' || t.category === filterCategory;
      return ms && mc;
    })
    .sort((a, b) => {
      let va = a[sortBy], vb = b[sortBy];
      if (sortBy === 'date') { va = new Date(a.date); vb = new Date(b.date); }
      if (sortBy === 'amount') { va = a.amount; vb = b.amount; }
      return sortDesc ? (vb > va ? 1 : -1) : (va > vb ? 1 : -1);
    });

  $: spending = transactions.filter(t => !t.is_credit);
  $: importable = includeCredits ? transactions : spending;
  $: totalSpend = spending.reduce((s, t) => s + t.amount, 0);
  $: categorisedCount = transactions.filter(t => t.category !== 'Uncategorized').length;

  $: summaryData = (() => {
    const m = {};
    for (const t of spending) { m[t.category] = (m[t.category] || 0) + t.amount; }
    return Object.entries(m).map(([cat, total]) => ({ cat, total })).sort((a, b) => b.total - a.total);
  })();

  // ── File upload ───────────────────────────────────────────────────────────────
  async function uploadFile(file) {
    if (!file) return;
    loading = true; parseError = ''; importResult = null; dryRunResult = null;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`${API}/parse`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      transactions = data.transactions.map((t, i) => ({ ...t, id: i }));
      detectedBank = data.bank || '';
      // Auto-build category map from Actual data when available
      if (actualBudgetLoaded) rebuildCategoryMap();
    } catch (e) { parseError = e.message; }
    finally { loading = false; }
  }

  function onDrop(e) {
    dragover = false;
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  }

  // ── Category editing ──────────────────────────────────────────────────────────
  function startEdit(t) { editingId = t.id; editingCategory = t.category; }
  function saveEdit(t) {
    transactions = transactions.map(tx => tx.id === t.id ? { ...tx, category: editingCategory } : tx);
    editingId = null;
  }

  // ── Category map builder ──────────────────────────────────────────────────────
  function rebuildCategoryMap() {
    const allCats = actualCategoryGroups.flatMap(g => g.categories || []);
    const payeeRuleCat = {};
    for (const rule of actualRules) {
      const ca = rule.actions?.find(a => a.field === 'category');
      const pc = rule.conditions?.find(c => c.field === 'payee');
      if (ca && pc) payeeRuleCat[pc.value] = ca.value;
    }
    const payeeNameId = {};
    for (const p of actualPayees) payeeNameId[p.name.toLowerCase()] = p.id;

    const ourCats = [...new Set(spending.map(t => t.category))];
    const map = {};
    for (const cat of ourCats) {
      const exact = allCats.find(c => c.name.toLowerCase() === cat.toLowerCase());
      if (exact) { map[cat] = exact.id; continue; }
      const words = cat.toLowerCase().split(/[\s&/]+/).filter(w => w.length > 3);
      let found = null;
      for (const [pname, pid] of Object.entries(payeeNameId)) {
        if (words.some(w => pname.includes(w)) && payeeRuleCat[pid]) {
          found = payeeRuleCat[pid]; break;
        }
      }
      map[cat] = found ?? '';
    }
    categoryMap = map;
  }

  // When Actual loads, rebuild map for any already-loaded transactions
  $: if (actualBudgetLoaded && spending.length) rebuildCategoryMap();

  // ── Import ────────────────────────────────────────────────────────────────────
  // Backend now generates imported_id (ref-based or hash); legacy ids cover older formats.
  function legacyStmtId(t) {
    return `stmt-${t.date}-${t.description}-${t.amount}`.replace(/\s+/g, '-');
  }

  // Old hash format: sha256("date|desc|abs(amount)")[:16] — kept so re-imports of
  // statements ingested before the sign/currency-aware hash still match existing rows.
  async function legacyHash(t) {
    const enc = new TextEncoder().encode(`${t.date}|${t.description}|${Math.abs(t.amount)}`);
    const buf = await crypto.subtle.digest('SHA-256', enc);
    return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
  }

  async function buildPayload(dr = false) {
    const rows = await Promise.all(importable.map(async t => ({
      ...t,
      category_id: categoryMap[t.category] || undefined,
      notes: t.category,
      legacy_ids: [legacyStmtId(t), await legacyHash(t)],
    })));
    return {
      accountId: actualAccountId,
      dryRun: dr,
      verified: verifications,
      transactions: rows,
    };
  }

  $: unresolvedVerify = (dryRunResult?.toVerify ?? []).filter(t => !verifications[t.imported_id]);
  $: hasUnresolved = unresolvedVerify.length > 0;

  async function runDryRun() {
    if (!actualAccountId) { importError = 'Select an account in the sidebar first'; return; }
    importing = true; importError = ''; dryRunResult = null; verifications = {};
    try {
      const payload = await buildPayload(true);
      const res = await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      dryRunResult = data;
    } catch (e) { importError = e.message; }
    finally { importing = false; }
  }

  async function doImport() {
    if (!actualAccountId) { importError = 'Select an account in the sidebar first'; return; }
    if (!confirmDestination) { importError = 'Tick "Yes, import to this account" to confirm the destination'; return; }
    importing = true; importError = ''; importResult = null;
    try {
      const payload = await buildPayload(false);
      const res = await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      importResult = data;
      confirmDestination = false;
    } catch (e) { importError = e.message; }
    finally { importing = false; }
  }

  // Reset confirmation whenever the destination account changes.
  $: { actualAccountId; confirmDestination = false; }

  $: destinationAccount = actualAccounts.find(a => a.id === actualAccountId);
  $: newCountEstimate = importable.length;
  function fmtAccountBalance(b) {
    if (b === null || b === undefined) return '';
    return (b / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' });
  }

  // ── CSV export ────────────────────────────────────────────────────────────────
  async function exportCSV() {
    const res = await fetch(`${API}/export/csv`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transactions: filtered })
    });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'budget_export.csv'; a.click();
    URL.revokeObjectURL(url);
  }

  // ── Formatters ────────────────────────────────────────────────────────────────
  function fmtAmt(n) { return n.toLocaleString('en-SG', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function fmtDate(d) { return new Date(d).toLocaleDateString('en-SG', { day: '2-digit', month: 'short', year: 'numeric' }); }
  function confClass(c) { return c >= 0.9 ? 'high' : c >= 0.75 ? 'med' : 'low'; }
</script>

<!-- Category Mapping Modal -->
{#if showMapper}
  <CategoryMapper
    {spending}
    {categoryMap}
    {actualCategoryGroups}
    on:save={e => { categoryMap = e.detail; showMapper = false; }}
    on:close={() => showMapper = false}
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
          <span class="badge">{detectedBank}</span>
        {/if}
      </div>
      {#if transactions.length}
        <div class="topbar-tabs">
          <button class="tab-btn" class:active={tab === 'transactions'} on:click={() => tab = 'transactions'}>
            Transactions <span class="badge muted">{transactions.length}</span>
          </button>
          <button class="tab-btn" class:active={tab === 'summary'} on:click={() => tab = 'summary'}>
            Summary
          </button>
        </div>
        <div class="topbar-right">
          <button class="ghost icon-btn" on:click={exportCSV} title="Export CSV">⬇ CSV</button>
          <button class="ghost icon-btn" on:click={() => { transactions = []; detectedBank = ''; importResult = null; dryRunResult = null; }}>
            ✕ Clear
          </button>
        </div>
      {/if}
    </header>

    <div class="content">
      <!-- ── EMPTY STATE / DROP ZONE ── -->
      {#if !transactions.length}
        <div class="upload-area">
          <!-- svelte-ignore a11y-no-static-element-interactions -->
          <div class="dropzone" class:dragover
            on:dragover|preventDefault={() => dragover = true}
            on:dragleave={() => dragover = false}
            on:drop|preventDefault={onDrop}
            on:click={() => document.getElementById('fi').click()}>
            {#if loading}
              <span class="spinner lg"></span>
              <p>Parsing statement…</p>
            {:else}
              <div class="drop-icon">📂</div>
              <p class="drop-title">Drop your bank statement here</p>
              <p class="drop-sub">or click to browse</p>
              <div class="banks">
                <span>UOB</span><span>DBS / POSB</span><span>OCBC</span><span>XLS / XLSX / PDF</span>
              </div>
            {/if}
          </div>
          <input id="fi" type="file" accept=".xls,.xlsx,.pdf" style="display:none"
            on:change={e => uploadFile(e.target.files[0])} />
          {#if parseError}
            <div class="error-msg">{parseError}</div>
          {/if}
        </div>

      <!-- ── TRANSACTIONS ── -->
      {:else if tab === 'transactions'}
        <!-- Import bar -->
        {#if actualBudgetLoaded}
          <div class="import-bar">
            <div class="import-bar-left">
              {#if importResult}
                <span class="success-msg">
                  ✓ Imported: {importResult.added} added, {importResult.updated} updated{importResult.skipped ? `, ${importResult.skipped} skipped` : ''}
                </span>
              {:else if dryRunResult}
                <span class="dryrun-msg">
                  🔍 Dry run: {dryRunResult.added} new, {dryRunResult.skipped} already imported{dryRunResult.toVerify?.length ? `, ${dryRunResult.toVerify.length} need review` : ''}
                </span>
              {:else if importError}
                <span class="error-msg">{importError}</span>
              {:else}
                <span class="import-hint">
                  {actualAccountId ? `Ready to import ${importable.length} transactions` : '← Select account in sidebar'}
                </span>
              {/if}
            </div>
            <div class="import-bar-right">
              <label class="checkbox-inline" title="Include deposit/refund rows alongside debits">
                <input type="checkbox" bind:checked={includeCredits} />
                Credits
              </label>
              <button class="ghost icon-btn" on:click={() => { rebuildCategoryMap(); showMapper = true; }}>
                🗂 Categories ({Object.values(categoryMap).filter(Boolean).length}/{[...new Set(importable.map(t=>t.category))].length} mapped)
              </button>
              <button class="ghost icon-btn" on:click={runDryRun} disabled={importing || !actualAccountId}>
                {importing ? '…' : '🔍 Dry Run'}
              </button>
              <button class="primary icon-btn" on:click={doImport} disabled={importing || !actualAccountId || hasUnresolved || !confirmDestination}>
                {importing ? '…' : hasUnresolved ? `⚠ Review ${unresolvedVerify.length} first` : '⬆ Import to Actual'}
              </button>
            </div>
          </div>

          {#if actualAccountId && destinationAccount}
            <div class="confirm-card">
              <div class="confirm-row">
                <span class="confirm-label">Destination</span>
                <strong>{destinationAccount.name}</strong>
                {#if destinationAccount.balance !== null && destinationAccount.balance !== undefined}
                  <span class="confirm-balance">· {fmtAccountBalance(destinationAccount.balance)}</span>
                {/if}
              </div>
              <div class="confirm-row counts">
                <span><strong>{dryRunResult?.added ?? newCountEstimate}</strong> new</span>
                <span><strong>{dryRunResult?.skipped ?? '—'}</strong> duplicate</span>
                <span><strong>{dryRunResult?.toVerify?.length ?? 0}</strong> need review</span>
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
              <span>⚠ {dryRunResult.toVerify.length} possible duplicate{dryRunResult.toVerify.length > 1 ? 's' : ''} — choose skip or import for each</span>
              <div class="verify-bulk">
                <button class="ghost icon-btn" on:click={() => { verifications = Object.fromEntries(dryRunResult.toVerify.map(t => [t.imported_id, 'skip'])); verifications = verifications; }}>Skip all</button>
                <button class="ghost icon-btn" on:click={() => { verifications = Object.fromEntries(dryRunResult.toVerify.map(t => [t.imported_id, 'import'])); verifications = verifications; }}>Import all</button>
              </div>
            </div>
            {#each dryRunResult.toVerify as t}
              <div class="verify-row" class:resolved={!!verifications[t.imported_id]}>
                <span class="verify-date">{t.date}</span>
                <span class="verify-desc">{t.description}</span>
                <span class="verify-amt">{t.currency} {t.amount.toFixed(2)}</span>
                <div class="verify-actions">
                  <button class="verify-btn" class:active={verifications[t.imported_id] === 'skip'}
                    on:click={() => { verifications[t.imported_id] = 'skip'; verifications = verifications; }}>Skip</button>
                  <button class="verify-btn import" class:active={verifications[t.imported_id] === 'import'}
                    on:click={() => { verifications[t.imported_id] = 'import'; verifications = verifications; }}>Import</button>
                </div>
              </div>
            {/each}
          </div>
        {/if}

        <!-- Filters -->
        <div class="toolbar">
          <input class="search-input" placeholder="🔍 Search…" bind:value={searchQuery} />
          <select bind:value={filterCategory}>
            <option value="All">All Categories</option>
            {#each CATEGORIES as c}<option value={c}>{c}</option>{/each}
          </select>
          <select bind:value={sortBy}>
            <option value="date">Date</option>
            <option value="amount">Amount</option>
            <option value="category">Category</option>
          </select>
          <button class="ghost icon-btn" on:click={() => sortDesc = !sortDesc}>{sortDesc ? '↓' : '↑'}</button>
          <label class="row-label">
            <input type="checkbox" bind:checked={showCredits} /> Credits
          </label>
        </div>

        <!-- Stats -->
        <div class="stats-row">
          <div class="stat-chip"><span>Showing</span><strong>{filtered.length}</strong></div>
          <div class="stat-chip"><span>Total Spend</span><strong>SGD {fmtAmt(totalSpend)}</strong></div>
          <div class="stat-chip"><span>Auto-categorised</span><strong>{categorisedCount}/{transactions.length}</strong></div>
        </div>

        <!-- Table -->
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Category</th>
                <th>Conf</th>
                <th class="r">Amount</th>
              </tr>
            </thead>
            <tbody>
              {#each filtered as t (t.id)}
                <tr class:credit={t.is_credit}>
                  <td class="td-date">{fmtDate(t.date)}</td>
                  <td class="td-desc">
                    <span>{t.description}</span>
                    {#if t.foreign_amount}
                      <span class="foreign">{t.foreign_currency} {t.foreign_amount}</span>
                    {/if}
                  </td>
                  <td>
                    {#if editingId === t.id}
                      <select bind:value={editingCategory}
                        on:change={() => saveEdit(t)} on:blur={() => saveEdit(t)}>
                        {#each CATEGORIES as c}<option value={c}>{c}</option>{/each}
                      </select>
                    {:else}
                      <button class="cat-badge"
                        style="background:{CAT_COLORS[t.category]}22;color:{CAT_COLORS[t.category]};border-color:{CAT_COLORS[t.category]}55"
                        on:click={() => startEdit(t)}>
                        {t.category} ✎
                      </button>
                    {/if}
                  </td>
                  <td>
                    {#if !t.is_credit}
                      <span class="conf {confClass(t.confidence)}">{Math.round(t.confidence*100)}%</span>
                    {/if}
                  </td>
                  <td class="r amt" class:credit-amt={t.is_credit}>
                    {t.is_credit ? '+' : ''}{t.currency} {fmtAmt(t.amount)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

      <!-- ── SUMMARY ── -->
      {:else if tab === 'summary'}
        <div class="summary">
          <div class="summary-header">
            <h2>Spending Summary</h2>
            <p>SGD {fmtAmt(totalSpend)} across {spending.length} transactions</p>
          </div>
          <div class="summary-grid">
            {#each summaryData as { cat, total }}
              <div class="summary-card">
                <div class="sc-top">
                  <span class="sc-dot" style="background:{CAT_COLORS[cat] ?? '#888'}"></span>
                  <span class="sc-name">{cat}</span>
                  <span class="sc-pct">{((total/totalSpend)*100).toFixed(1)}%</span>
                </div>
                <div class="sc-bar-bg">
                  <div class="sc-bar" style="width:{(total/totalSpend)*100}%;background:{CAT_COLORS[cat] ?? '#888'}"></div>
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
  .app { display: flex; height: 100vh; overflow: hidden; }
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }

  /* Top bar */
  .topbar {
    display: flex; align-items: center; gap: 12px; padding: 0 20px;
    height: 52px; background: var(--surface); border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .topbar-left { display: flex; align-items: center; gap: 10px; flex: 1; }
  .app-title { font-size: 16px; font-weight: 700; }
  .topbar-tabs { display: flex; gap: 2px; }
  .tab-btn {
    background: transparent; border: none; color: var(--text2);
    padding: 6px 14px; border-radius: 6px; font-size: 13px; font-weight: 500;
  }
  .tab-btn:hover, .tab-btn.active { background: var(--surface2); color: var(--text); }
  .tab-btn.active { color: var(--accent); }
  .topbar-right { display: flex; gap: 8px; }

  /* Content area */
  .content { flex: 1; overflow-y: auto; padding: 0; }

  /* Upload */
  .upload-area { display: flex; flex-direction: column; gap: 16px; align-items: center; justify-content: center; height: 100%; padding: 40px; }
  .dropzone {
    width: 100%; max-width: 480px; border: 2px dashed var(--border); border-radius: 16px;
    padding: 60px 40px; cursor: pointer; text-align: center; transition: all 0.2s;
    display: flex; flex-direction: column; align-items: center; gap: 12px;
    background: var(--surface);
  }
  .dropzone.dragover { border-color: var(--accent); background: var(--surface2); }
  .drop-icon { font-size: 48px; }
  .drop-title { font-size: 18px; font-weight: 600; }
  .drop-sub { color: var(--text2); font-size: 13px; }
  .banks { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 8px; }
  .banks span { background: var(--surface2); border: 1px solid var(--border); border-radius: 999px; padding: 3px 12px; font-size: 12px; color: var(--text2); }

  /* Import bar */
  .import-bar {
    display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;
    padding: 10px 20px; background: var(--surface); border-bottom: 1px solid var(--border);
  }
  .import-bar-left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
  .import-bar-right { display: flex; gap: 8px; flex-shrink: 0; }
  .import-hint { font-size: 13px; color: var(--text2); }
  .dryrun-msg { font-size: 13px; color: var(--accent); background: #6c63ff11; border: 1px solid #6c63ff33; border-radius: 6px; padding: 5px 10px; }
  .checkbox-inline { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text2); user-select: none; }
  .checkbox-inline input { margin: 0; }

  /* Destination confirmation card */
  .confirm-card {
    margin: 10px 20px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px;
    background: var(--surface); display: flex; flex-direction: column; gap: 8px;
  }
  .confirm-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text2); flex-wrap: wrap; }
  .confirm-row strong { color: var(--text); }
  .confirm-label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text2); }
  .confirm-balance { color: var(--text2); }
  .confirm-row.counts { gap: 16px; }
  .confirm-check { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; user-select: none; padding-top: 4px; border-top: 1px dashed var(--border); }
  .confirm-check input { margin: 0; }

  /* Verification panel */
  .verify-panel {
    border-bottom: 1px solid var(--border); background: #ff980011;
  }
  .verify-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 20px; font-size: 13px; color: #b45309; font-weight: 500;
    background: #ff980018; border-bottom: 1px solid #ff980033;
  }
  .verify-bulk { display: flex; gap: 6px; }
  .verify-row {
    display: flex; align-items: center; gap: 12px; padding: 8px 20px;
    border-bottom: 1px solid var(--border); font-size: 13px;
  }
  .verify-row.resolved { opacity: 0.6; }
  .verify-date { color: var(--text2); min-width: 90px; flex-shrink: 0; }
  .verify-desc { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .verify-amt { min-width: 90px; text-align: right; font-weight: 500; flex-shrink: 0; }
  .verify-actions { display: flex; gap: 4px; flex-shrink: 0; }
  .verify-btn {
    padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: 500;
    border: 1px solid var(--border); background: var(--surface); color: var(--text2); cursor: pointer;
  }
  .verify-btn.active { background: var(--surface2); color: var(--text); border-color: var(--text2); }
  .verify-btn.import.active { background: #6c63ff22; color: var(--accent); border-color: var(--accent); }

  /* Toolbar */
  .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 10px 20px; border-bottom: 1px solid var(--border); }
  .search-input { flex: 1; min-width: 160px; }

  /* Stats */
  .stats-row { display: flex; gap: 10px; padding: 10px 20px; flex-wrap: wrap; }
  .stat-chip { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 6px 14px; display: flex; flex-direction: column; gap: 1px; }
  .stat-chip span { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: .04em; }
  .stat-chip strong { font-size: 16px; font-weight: 700; }

  /* Table */
  .table-wrap { overflow-x: auto; padding: 0 20px 20px; }
  table { width: 100%; border-collapse: collapse; }
  thead { position: sticky; top: 0; background: var(--surface2); z-index: 1; }
  th { padding: 10px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text2); font-weight: 600; border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; font-size: 13px; }
  tr:hover td { background: var(--surface2); }
  tr.credit td { opacity: 0.5; }
  .r { text-align: right; }

  .td-date { white-space: nowrap; color: var(--text2); }
  .td-desc { max-width: 260px; }
  .td-desc span { display: block; }
  .foreign { color: var(--text2); font-size: 12px; }

  .cat-badge {
    padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 500;
    border: 1px solid; background: transparent; white-space: nowrap; cursor: pointer;
  }
  .cat-badge:hover { opacity: 0.8; }

  .conf { font-size: 11px; padding: 2px 7px; border-radius: 999px; }
  .conf.high { background: #00c9a722; color: var(--accent2); }
  .conf.med  { background: #f7931e22; color: var(--warn); }
  .conf.low  { background: #ff6b6b22; color: var(--danger); }

  .amt { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .credit-amt { color: var(--accent2); }

  /* Summary */
  .summary { padding: 24px 20px; }
  .summary-header { margin-bottom: 20px; }
  .summary-header h2 { font-size: 20px; font-weight: 700; }
  .summary-header p { color: var(--text2); font-size: 13px; margin-top: 4px; }
  .summary-grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
  .summary-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; }
  .sc-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .sc-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .sc-name { flex: 1; font-weight: 500; font-size: 13px; }
  .sc-pct { font-size: 12px; color: var(--text2); }
  .sc-bar-bg { height: 5px; background: var(--surface2); border-radius: 999px; overflow: hidden; margin-bottom: 8px; }
  .sc-bar { height: 100%; border-radius: 999px; }
  .sc-amt { font-size: 18px; font-weight: 700; }
</style>
