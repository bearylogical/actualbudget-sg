<script>
  import { onMount } from 'svelte';
  export let transactions = [];

  const API = '/api';
  const STORAGE_KEY = 'budget-parser-actual-conn';

  // ── connection state ────────────────────────────────────────────────────────
  let serverURL = '';
  let password = '';
  let encryptionPassword = '';
  let useEncryption = false;

  // ── actual data ─────────────────────────────────────────────────────────────
  let budgets = [];
  let selectedBudgetId = '';
  let accounts = [];
  let categoryGroups = [];
  let payees = [];
  let rules = [];
  let selectedAccountId = '';

  // ── category mapping ────────────────────────────────────────────────────────
  let categoryMap = {};    // ourLabel → actualCategoryId
  let createMissing = {};  // ourLabel → bool

  // ── import options ──────────────────────────────────────────────────────────
  let learnCategories = true;
  let includeCredits = false;
  let createPayeeRules = true;

  // ── split transactions ──────────────────────────────────────────────────────
  // Map: txn imported_id → [{amount, category_id, notes}]
  let splits = {};
  let splitEditing = null; // imported_id of txn currently being split

  // ── budget month viewer ─────────────────────────────────────────────────────
  let budgetMonth = currentMonth();
  let budgetMonthData = null;
  let loadingBudgetMonth = false;

  // ── UI state ─────────────────────────────────────────────────────────────────
  let step = 'connect';
  let activeTab = 'import'; // 'import' | 'budget'  (shown after budget loaded)
  let loading = false;
  let error = '';
  let importResult = null;
  let previewResult = null;
  let loadingPreview = false;
  let dryRunResult = null;

  const STEPS = ['connect','budget','account','mapping','done'];
  const STEP_LABELS = { connect:'Connect', budget:'Budget', account:'Account', mapping:'Categories', done:'Done' };

  // ── derived ─────────────────────────────────────────────────────────────────
  $: spendingTxns = includeCredits ? transactions : transactions.filter(t => !t.is_credit);
  $: ourCategories = [...new Set(spendingTxns.map(t => t.category))].sort();
  $: allActualCats = categoryGroups.flatMap(g => g.categories.map(c => ({ ...c, groupName: g.name })));
  $: selectedAccount = accounts.find(a => a.id === selectedAccountId);
  $: alreadyExistCount = previewResult
    ? spendingTxns.filter(t => previewResult.existingIds.includes(importedId(t))).length
    : null;
  $: newCount = alreadyExistCount !== null ? spendingTxns.length - alreadyExistCount : spendingTxns.length;
  $: unmappedCategories = ourCategories.filter(c => !categoryMap[c]);

  // ── helpers ─────────────────────────────────────────────────────────────────
  function currentMonth() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  function importedId(t) {
    return `uob-${t.date}-${t.description}-${t.amount}`.replace(/\s+/g, '-');
  }

  function fmtBalance(b) {
    if (b === null || b === undefined) return '';
    return `SGD ${(b / 100).toLocaleString('en-SG', { minimumFractionDigits: 2 })}`;
  }

  function fmtAmt(n) {
    return n.toLocaleString('en-SG', { minimumFractionDigits: 2 });
  }

  function saveConn() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ serverURL, useEncryption })); } catch {}
  }
  function loadConn() {
    try {
      const s = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      if (s.serverURL) serverURL = s.serverURL;
      if (s.useEncryption) useEncryption = s.useEncryption;
    } catch {}
  }
  onMount(loadConn);

  // Build category pre-map: name match, then payee rule inference
  function buildCategoryMap(ourCats, actualCats, payeeList, ruleList) {
    const map = {};
    // payeeId → categoryId from rules
    const payeeRuleCat = {};
    for (const rule of ruleList) {
      const catAction = rule.actions?.find(a => a.field === 'category');
      const payeeCond = rule.conditions?.find(c => c.field === 'payee');
      if (catAction && payeeCond) payeeRuleCat[payeeCond.value] = catAction.value;
    }
    // payee name lower → payee id
    const payeeNameId = {};
    for (const p of payeeList) payeeNameId[p.name.toLowerCase()] = p.id;

    for (const ourCat of ourCats) {
      const exact = actualCats.find(c => c.name.toLowerCase() === ourCat.toLowerCase());
      if (exact) { map[ourCat] = exact.id; continue; }

      const words = ourCat.toLowerCase().split(/[\s&/]+/).filter(w => w.length > 3);
      let found = null;
      for (const [pname, pid] of Object.entries(payeeNameId)) {
        if (words.some(w => pname.includes(w))) {
          if (payeeRuleCat[pid]) { found = payeeRuleCat[pid]; break; }
        }
      }
      map[ourCat] = found ?? '';
    }
    return map;
  }

  // ── split helpers ────────────────────────────────────────────────────────────
  function initSplitForTxn(t) {
    const id = importedId(t);
    if (!splits[id]) {
      splits[id] = [
        { amount: t.amount, category_id: categoryMap[t.category] || '', notes: t.category },
        { amount: 0, category_id: '', notes: '' }
      ];
      splits = splits;
    }
  }

  function getSplitLines(id) {
    return splits[id] || [];
  }

  function addSplitLine(id) {
    splits[id] = [...splits[id], { amount: 0, category_id: '', notes: '' }];
    splits = splits;
  }

  function removeSplitLine(id, idx) {
    splits[id] = splits[id].filter((_, i) => i !== idx);
    splits = splits;
  }

  function splitTotal(id) {
    return (splits[id] || []).reduce((s, l) => s + Number(l.amount || 0), 0);
  }

  // ── API calls ────────────────────────────────────────────────────────────────
  async function connect() {
    if (!serverURL || !password) { error = 'Server URL and password required'; return; }
    loading = true; error = '';
    try {
      const res = await fetch(`${API}/actual/budgets`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ serverURL, password })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      budgets = data.budgets;
      saveConn();
      step = 'budget';
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }

  async function loadBudget() {
    if (!selectedBudgetId) { error = 'Select a budget'; return; }
    loading = true; error = '';
    try {
      const body = { serverURL, password, budgetId: selectedBudgetId };
      if (useEncryption && encryptionPassword) body.encryptionPassword = encryptionPassword;
      const res = await fetch(`${API}/actual/budgets/load`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      accounts = data.accounts;
      categoryGroups = data.categoryGroups;
      payees = data.payees ?? [];
      rules = data.rules ?? [];
      categoryMap = buildCategoryMap(ourCategories, allActualCats, payees, rules);
      createMissing = Object.fromEntries(ourCategories.map(c => [c, false]));
      step = 'account';
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }

  async function fetchPreview() {
    if (!selectedAccountId || !spendingTxns.length) return;
    loadingPreview = true;
    try {
      const dates = spendingTxns.map(t => t.date).sort();
      const res = await fetch(`${API}/actual/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accountId: selectedAccountId, startDate: dates[0], endDate: dates[dates.length - 1] })
      });
      if (res.ok) previewResult = await res.json();
    } catch {}
    finally { loadingPreview = false; }
  }

  async function runDryRun() {
    if (!selectedAccountId) { error = 'Select an account first'; return; }
    loading = true; error = ''; dryRunResult = null;
    try {
      const enriched = buildEnrichedTxns();
      const res = await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accountId: selectedAccountId, transactions: enriched, learnCategories: false, dryRun: true })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      dryRunResult = data;
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }

  async function createMissingCategories() {
    const toCreate = ourCategories.filter(c => createMissing[c] && !categoryMap[c]);
    for (const name of toCreate) {
      const res = await fetch(`${API}/actual/categories`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, groupName: 'Imported' })
      });
      const data = await res.json();
      if (res.ok) categoryMap = { ...categoryMap, [name]: data.id };
    }
    if (toCreate.length) {
      const catRes = await fetch(`${API}/actual/categories`);
      if (catRes.ok) { const d = await catRes.json(); categoryGroups = d.categoryGroups; }
    }
  }

  async function createRulesForMappings() {
    const mappings = ourCategories
      .filter(c => categoryMap[c])
      .map(c => ({ description: c, categoryId: categoryMap[c] }));
    if (!mappings.length) return;
    await fetch(`${API}/actual/rules`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mappings })
    });
  }

  function buildEnrichedTxns() {
    return spendingTxns.map(t => {
      const id = importedId(t);
      const hasSplit = splits[id] && splits[id].length > 1;
      return {
        ...t,
        category_id: categoryMap[t.category] || undefined,
        notes: t.category,
        imported_id: id,
        ...(hasSplit ? { splits: splits[id] } : {}),
      };
    });
  }

  async function doImport() {
    if (!selectedAccountId) { error = 'Select an account'; return; }
    loading = true; error = '';
    step = 'importing';
    try {
      await createMissingCategories();
      const enriched = buildEnrichedTxns();
      const res = await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accountId: selectedAccountId, transactions: enriched, learnCategories, dryRun: false })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);

      if (createPayeeRules) await createRulesForMappings();

      const acctRes = await fetch(`${API}/actual/accounts`);
      if (acctRes.ok) { const d = await acctRes.json(); accounts = d.accounts; }

      importResult = data;
      step = 'done';
    } catch (e) { error = e.message; step = 'mapping'; }
    finally { loading = false; }
  }

  async function fetchBudgetMonth() {
    if (!budgetMonth) return;
    loadingBudgetMonth = true; budgetMonthData = null;
    try {
      const res = await fetch(`${API}/actual/budget-month/${budgetMonth}`);
      if (res.ok) budgetMonthData = await res.json();
    } catch {}
    finally { loadingBudgetMonth = false; }
  }

  async function reset() {
    await fetch(`${API}/actual/reset`, { method: 'POST' });
    step = 'connect'; activeTab = 'import';
    budgets = []; accounts = []; categoryGroups = []; payees = []; rules = [];
    selectedBudgetId = ''; selectedAccountId = '';
    categoryMap = {}; createMissing = {}; splits = {};
    previewResult = null; importResult = null; dryRunResult = null; error = '';
  }

  $: if (selectedAccountId) fetchPreview();
  // Budget month is fetched explicitly on tab click and done-screen button

  // Budget month helpers
  function budgetCatRows(data) {
    if (!data?.budget?.categoryGroups) return [];
    return data.budget.categoryGroups.flatMap(g =>
      (g.categories || []).map(c => ({
        group: g.name,
        name: c.name,
        budgeted: c.budgeted ?? 0,
        spent: c.spent ?? 0,
        balance: c.balance ?? 0,
      }))
    ).filter(r => r.budgeted !== 0 || r.spent !== 0);
  }
</script>

<div class="panel">
  <!-- Step indicator (only shown during import flow) -->
  {#if activeTab === 'import'}
  <div class="stepper">
    {#each STEPS as s}
      <div class="step-item"
        class:active={step === s || (step === 'importing' && s === 'mapping')}
        class:past={STEPS.indexOf(step) > STEPS.indexOf(s)}>
        <div class="step-dot"></div>
        <span>{STEP_LABELS[s]}</span>
      </div>
      {#if s !== 'done'}<div class="step-line"></div>{/if}
    {/each}
  </div>
  {/if}

  <!-- Tab bar (shown once budget is loaded) -->
  {#if step !== 'connect' && step !== 'budget'}
  <div class="tab-bar">
    <button class="tab" class:active={activeTab === 'import'} on:click={() => activeTab = 'import'}>⬆ Import</button>
    <button class="tab" class:active={activeTab === 'budget'} on:click={() => { activeTab = 'budget'; fetchBudgetMonth(); }}>📊 Budget Month</button>
  </div>
  {/if}

  {#if error}
    <div class="error-box">⚠️ {error} <button class="clear-err" on:click={() => error = ''}>✕</button></div>
  {/if}

  <!-- ═══════════════════════════════════════════════════════ BUDGET MONTH TAB -->
  {#if activeTab === 'budget' && step !== 'connect' && step !== 'budget'}
    <div class="form-section">
      <div class="month-header">
        <h3>Budget Month</h3>
        <div class="month-nav">
          <button class="ghost sm" on:click={() => {
            const [y,m] = budgetMonth.split('-').map(Number);
            const d = new Date(y, m-2); budgetMonth = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
          }}>←</button>
          <input type="month" bind:value={budgetMonth} on:change={fetchBudgetMonth} />
          <button class="ghost sm" on:click={() => {
            const [y,m] = budgetMonth.split('-').map(Number);
            const d = new Date(y, m); budgetMonth = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;
          }}>→</button>
          <button class="ghost sm" on:click={fetchBudgetMonth}>↻</button>
        </div>
      </div>

      {#if loadingBudgetMonth}
        <div class="centered"><div class="spinner-sm"></div></div>
      {:else if budgetMonthData}
        {@const rows = budgetCatRows(budgetMonthData)}
        {@const totalBudgeted = rows.reduce((s,r) => s + r.budgeted, 0)}
        {@const totalSpent = rows.reduce((s,r) => s + r.spent, 0)}
        <div class="bm-summary">
          <div class="bm-stat">
            <span>Budgeted</span>
            <strong>{fmtBalance(totalBudgeted)}</strong>
          </div>
          <div class="bm-stat">
            <span>Spent</span>
            <strong class:over={totalSpent < totalBudgeted}>{fmtBalance(Math.abs(totalSpent))}</strong>
          </div>
          <div class="bm-stat">
            <span>Remaining</span>
            <strong class:over={(totalBudgeted + totalSpent) < 0}>{fmtBalance(totalBudgeted + totalSpent)}</strong>
          </div>
        </div>
        <div class="bm-table-wrap">
          <table class="bm-table">
            <thead><tr><th>Category</th><th>Budgeted</th><th>Spent</th><th>Remaining</th></tr></thead>
            <tbody>
              {#each rows as row}
                {@const remaining = row.budgeted + row.spent}
                {@const pct = row.budgeted ? Math.min(100, Math.abs(row.spent) / row.budgeted * 100) : 0}
                <tr>
                  <td>
                    <div class="cat-name-cell">{row.name}</div>
                    <div class="bm-bar-wrap"><div class="bm-bar" class:over={remaining < 0} style="width:{pct}%"></div></div>
                  </td>
                  <td class="num">{fmtBalance(row.budgeted)}</td>
                  <td class="num spent">{fmtBalance(Math.abs(row.spent))}</td>
                  <td class="num" class:over={remaining < 0}>{fmtBalance(remaining)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="hint">No budget data for {budgetMonth}.</p>
      {/if}
    </div>

  <!-- ═══════════════════════════════════════════════════════ IMPORT TAB / STEPS -->
  {:else if activeTab === 'import'}

  <!-- ── CONNECT ── -->
  {#if step === 'connect'}
    <div class="form-section">
      <h3>Connect to Actual Budget</h3>
      <p class="hint">Your password is used to connect and is never stored locally.</p>
      <label>Server URL
        <input bind:value={serverURL} placeholder="http://192.168.1.x:5006"
          on:keydown={e => e.key === 'Enter' && connect()} />
      </label>
      <label>Password
        <input type="password" bind:value={password} placeholder="Your Actual password"
          on:keydown={e => e.key === 'Enter' && connect()} />
      </label>
      <label class="checkbox-label">
        <input type="checkbox" bind:checked={useEncryption} /> Budget uses end-to-end encryption
      </label>
      {#if useEncryption}
        <label>Encryption Password
          <input type="password" bind:value={encryptionPassword} />
        </label>
      {/if}
      <button class="primary" on:click={connect} disabled={loading}>
        {loading ? 'Connecting…' : 'Connect →'}
      </button>
    </div>

  <!-- ── SELECT BUDGET ── -->
  {:else if step === 'budget'}
    <div class="form-section">
      <h3>Select Budget File</h3>
      <div class="item-list">
        {#each budgets as b}
          <button class="list-item" class:selected={selectedBudgetId === b.groupId}
            on:click={() => selectedBudgetId = b.groupId}>
            <span class="item-icon">📒</span>
            <div class="item-body">
              <span class="item-name">{b.name}</span>
              <span class="item-sub">{b.state === 'remote' ? '☁ Remote' : '💾 Local'}</span>
            </div>
            {#if selectedBudgetId === b.groupId}<span class="check">✓</span>{/if}
          </button>
        {/each}
        {#if !budgets.length}<p class="hint">No budget files found.</p>{/if}
      </div>
      <div class="row">
        <button class="ghost" on:click={() => step = 'connect'}>← Back</button>
        <button class="primary" on:click={loadBudget} disabled={loading || !selectedBudgetId}>
          {loading ? 'Loading…' : 'Load Budget →'}
        </button>
      </div>
    </div>

  <!-- ── SELECT ACCOUNT ── -->
  {:else if step === 'account'}
    <div class="form-section">
      <h3>Select Account</h3>
      <p class="hint">Which account should these transactions be imported into?</p>
      <div class="item-list">
        {#each accounts.filter(a => !a.closed) as acct}
          <button class="list-item" class:selected={selectedAccountId === acct.id}
            on:click={() => selectedAccountId = acct.id}>
            <span class="item-icon">🏦</span>
            <div class="item-body">
              <span class="item-name">{acct.name}</span>
              {#if acct.balance !== null && acct.balance !== undefined}
                <span class="item-sub">Balance: {fmtBalance(acct.balance)}</span>
              {/if}
            </div>
            <span class="item-type">{acct.type ?? ''}</span>
            {#if selectedAccountId === acct.id}<span class="check">✓</span>{/if}
          </button>
        {/each}
      </div>

      {#if selectedAccountId}
        <div class="preview-box">
          {#if loadingPreview}
            <span class="hint">Checking existing transactions…</span>
          {:else if previewResult}
            <div class="preview-row"><span>📥 New to import</span><strong>{newCount}</strong></div>
            {#if alreadyExistCount > 0}
              <div class="preview-row muted"><span>⏭ Already in Actual</span><strong>{alreadyExistCount}</strong></div>
            {/if}
          {/if}
        </div>

        {#if dryRunResult}
          <div class="dryrun-box">
            <strong>Dry run result:</strong> would add {dryRunResult.added}, update {dryRunResult.updated}
          </div>
        {/if}
      {/if}

      <div class="options-box">
        <label class="checkbox-label">
          <input type="checkbox" bind:checked={includeCredits} />
          Include credit entries (payments/refunds)
        </label>
        <label class="checkbox-label">
          <input type="checkbox" bind:checked={learnCategories} />
          Update Actual rules from imported categories
        </label>
        <label class="checkbox-label">
          <input type="checkbox" bind:checked={createPayeeRules} />
          Create payee→category rules after import
        </label>
      </div>

      <div class="row">
        <button class="ghost" on:click={() => step = 'budget'}>← Back</button>
        <button class="ghost" on:click={runDryRun} disabled={loading || !selectedAccountId}>
          {loading ? '…' : '🔍 Dry Run'}
        </button>
        <button class="ghost" on:click={() => { if (selectedAccountId) step = 'mapping'; else error = 'Select an account first'; }}>
          Map Categories →
        </button>
        <button class="primary" on:click={doImport} disabled={loading || !selectedAccountId}>
          Import {spendingTxns.length} →
        </button>
      </div>
    </div>

  <!-- ── CATEGORY MAPPING ── -->
  {:else if step === 'mapping'}
    <div class="form-section">
      <h3>Category Mapping</h3>
      <p class="hint">
        <strong>{ourCategories.filter(c => categoryMap[c]).length}/{ourCategories.length}</strong>
        auto-matched from Actual's categories and payee rules.
      </p>

      <div class="mapping-table">
        <div class="mapping-header">
          <span>Parsed Category</span>
          <span>→ Actual Category</span>
          <span>Create</span>
        </div>
        {#each ourCategories as cat}
          {@const txnCount = spendingTxns.filter(t => t.category === cat).length}
          <div class="mapping-row" class:unmapped={!categoryMap[cat]}>
            <div class="our-cat">
              <span>{cat}</span>
              <span class="txn-count">{txnCount} txn{txnCount !== 1 ? 's' : ''}</span>
            </div>
            <select bind:value={categoryMap[cat]}>
              <option value="">— skip —</option>
              {#each categoryGroups as group}
                <optgroup label={group.name}>
                  {#each group.categories as c}
                    <option value={c.id}>{c.name}</option>
                  {/each}
                </optgroup>
              {/each}
            </select>
            {#if !categoryMap[cat]}
              <label class="checkbox-label mini">
                <input type="checkbox" bind:checked={createMissing[cat]} /> Create
              </label>
            {:else}
              <span class="matched">✓</span>
            {/if}
          </div>
        {/each}
      </div>

      <!-- Split transaction editor -->
      <details class="split-section">
        <summary>✂️ Split transactions ({Object.keys(splits).length} configured)</summary>
        <div class="split-list">
          {#each spendingTxns as t}
            {@const id = importedId(t)}
            <div class="split-txn">
              <div class="split-txn-header">
                <span class="split-desc">{t.description}</span>
                <span class="split-date">{t.date}</span>
                <span class="split-total-amt">SGD {fmtAmt(t.amount)}</span>
                <button class="ghost sm" on:click={() => splitEditing = splitEditing === id ? null : id}>
                  {splitEditing === id ? 'Done' : 'Split'}
                </button>
              </div>
              {#if splitEditing === id}
                {@const lines = getSplitLines(id)}
                <div class="split-lines">
                  {#each lines as line, idx}
                    <div class="split-line">
                      <input type="number" step="0.01" bind:value={line.amount} placeholder="Amount" />
                      <select bind:value={line.category_id}>
                        <option value="">— no category —</option>
                        {#each categoryGroups as group}
                          <optgroup label={group.name}>
                            {#each group.categories as c}
                              <option value={c.id}>{c.name}</option>
                            {/each}
                          </optgroup>
                        {/each}
                      </select>
                      <input bind:value={line.notes} placeholder="Note" />
                      {#if lines.length > 1}
                        <button class="ghost sm danger" on:click={() => removeSplitLine(id, idx)}>✕</button>
                      {/if}
                    </div>
                  {/each}
                  <div class="split-footer">
                    <button class="ghost sm" on:click={() => addSplitLine(id)}>+ Add line</button>
                    <span class:split-warn={Math.abs(splitTotal(id) - t.amount) > 0.01}>
                      Total: SGD {fmtAmt(splitTotal(id))} / {fmtAmt(t.amount)}
                    </span>
                  </div>
                </div>
              {/if}
            </div>
          {/each}
        </div>
      </details>

      {#if unmappedCategories.length}
        <p class="hint warning">
          ⚠️ {unmappedCategories.filter(c => !createMissing[c]).length} categories will import without a category tag.
        </p>
      {/if}

      <div class="row">
        <button class="ghost" on:click={() => step = 'account'}>← Back</button>
        <button class="primary" on:click={doImport} disabled={loading}>
          {loading ? 'Importing…' : `Import ${spendingTxns.length} →`}
        </button>
      </div>
    </div>

  <!-- ── IMPORTING ── -->
  {:else if step === 'importing'}
    <div class="form-section centered">
      <div class="big-spinner"></div>
      <p>Importing transactions into Actual…</p>
    </div>

  <!-- ── DONE ── -->
  {:else if step === 'done'}
    <div class="form-section centered">
      <div class="success-icon">✅</div>
      <h3>Import Complete</h3>
      {#if importResult}
        <div class="result-stats">
          <div class="result-stat"><span class="result-n">{importResult.added}</span><span>Added</span></div>
          <div class="result-stat"><span class="result-n">{importResult.updated}</span><span>Updated</span></div>
        </div>
        {#if importResult.errors?.length}
          <p class="hint warning">⚠️ {importResult.errors.length} error(s) during import.</p>
        {/if}
      {/if}
      {#if selectedAccount}
        <div class="account-balance-post">
          <span class="hint">New balance: <strong>{selectedAccount.name}</strong></span>
          <span class="balance-value">{fmtBalance(selectedAccount.balance)}</span>
        </div>
      {/if}
      <div class="row" style="justify-content:center; margin-top:8px">
        <button class="ghost" on:click={() => { activeTab = 'budget'; fetchBudgetMonth(); }}>
          📊 View Budget Month
        </button>
        <button class="ghost" on:click={reset}>← Import Another</button>
      </div>
    </div>
  {/if}

  {/if} <!-- end activeTab === import -->
</div>

<style>
  .panel { display: flex; flex-direction: column; gap: 20px; }

  /* Stepper */
  .stepper { display: flex; align-items: center; padding: 16px 0; overflow-x: auto; }
  .step-item { display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 64px; }
  .step-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--border); border: 2px solid var(--border); transition: all 0.2s; }
  .step-item.active .step-dot { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 0 3px #6c63ff33; }
  .step-item.past .step-dot { background: var(--accent2); border-color: var(--accent2); }
  .step-item span { font-size: 11px; color: var(--text2); }
  .step-item.active span { color: var(--text); }
  .step-line { flex: 1; height: 2px; background: var(--border); min-width: 16px; }

  /* Tab bar */
  .tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); padding-bottom: 0; }
  .tab { background: transparent; color: var(--text2); padding: 8px 18px; border: none; border-bottom: 2px solid transparent; border-radius: 0; font-size: 13px; font-weight: 500; margin-bottom: -1px; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

  /* Error */
  .error-box { display: flex; justify-content: space-between; align-items: center; background: #ff6b6b22; border: 1px solid #ff6b6b44; color: #ff6b6b; border-radius: 8px; padding: 10px 14px; }
  .clear-err { background: none; border: none; color: #ff6b6b; font-size: 16px; padding: 0 4px; cursor: pointer; }

  /* Form */
  .form-section { display: flex; flex-direction: column; gap: 14px; }
  .form-section h3 { font-size: 18px; font-weight: 700; }
  .hint { color: var(--text2); font-size: 13px; line-height: 1.6; }
  .hint.warning { color: #f7931e; }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; font-weight: 500; color: var(--text2); }
  .checkbox-label { flex-direction: row; align-items: center; gap: 10px; cursor: pointer; font-weight: 400; color: var(--text); }
  .checkbox-label input[type=checkbox] { width: auto; }
  .checkbox-label.mini { font-size: 12px; }

  /* Item lists */
  .item-list { display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; }
  .list-item { display: flex; align-items: center; gap: 10px; padding: 11px 14px; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; text-align: left; color: var(--text); transition: all 0.15s; }
  .list-item:hover { border-color: var(--accent); }
  .list-item.selected { border-color: var(--accent); background: #6c63ff11; }
  .item-icon { font-size: 18px; flex-shrink: 0; }
  .item-body { flex: 1; display: flex; flex-direction: column; gap: 2px; }
  .item-name { font-weight: 500; }
  .item-sub { font-size: 12px; color: var(--text2); }
  .item-type { font-size: 12px; color: var(--text2); }
  .check { color: var(--accent2); font-weight: 700; flex-shrink: 0; }

  /* Preview / dry run */
  .preview-box { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
  .preview-row { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
  .preview-row.muted { opacity: 0.6; }
  .dryrun-box { background: #6c63ff11; border: 1px solid #6c63ff44; border-radius: 8px; padding: 10px 14px; font-size: 13px; color: var(--accent); }

  /* Options */
  .options-box { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }

  /* Buttons */
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row button { flex: 1; }
  button.sm { padding: 4px 10px; font-size: 12px; flex: none; }
  button.danger { color: var(--danger); border-color: #ff6b6b44; }

  /* Mapping */
  .mapping-table { display: flex; flex-direction: column; gap: 6px; max-height: 300px; overflow-y: auto; }
  .mapping-header { display: grid; grid-template-columns: 1.2fr 1.5fr 0.5fr; gap: 10px; font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; padding: 0 4px; }
  .mapping-row { display: grid; grid-template-columns: 1.2fr 1.5fr 0.5fr; gap: 10px; align-items: center; padding: 4px 0; border-radius: 6px; }
  .mapping-row.unmapped { background: #f7931e08; }
  .our-cat { display: flex; flex-direction: column; gap: 2px; padding: 6px 10px; background: var(--surface2); border-radius: 6px; border: 1px solid var(--border); }
  .txn-count { font-size: 11px; color: var(--text2); }
  .mapping-row select { width: 100%; }
  .matched { color: var(--accent2); font-weight: 700; text-align: center; }

  /* Split editor */
  .split-section { border: 1px solid var(--border); border-radius: 10px; }
  .split-section summary { padding: 10px 14px; cursor: pointer; font-size: 13px; font-weight: 500; list-style: none; }
  .split-section summary::-webkit-details-marker { display: none; }
  .split-list { padding: 0 14px 14px; display: flex; flex-direction: column; gap: 12px; max-height: 360px; overflow-y: auto; }
  .split-txn { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .split-txn-header { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: var(--surface2); font-size: 13px; }
  .split-desc { flex: 1; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .split-date { color: var(--text2); font-size: 12px; flex-shrink: 0; }
  .split-total-amt { font-weight: 600; flex-shrink: 0; }
  .split-lines { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
  .split-line { display: grid; grid-template-columns: 100px 1fr 1fr auto; gap: 8px; align-items: center; }
  .split-line input, .split-line select { font-size: 12px; padding: 5px 8px; }
  .split-footer { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--text2); padding-top: 4px; }
  .split-warn { color: var(--danger); font-weight: 600; }

  /* Centered states */
  .centered { align-items: center; text-align: center; padding: 20px 0; }
  .big-spinner { width: 52px; height: 52px; border: 4px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
  .spinner-sm { width: 28px; height: 28px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .success-icon { font-size: 52px; }
  .result-stats { display: flex; gap: 32px; margin: 10px 0; }
  .result-stat { display: flex; flex-direction: column; align-items: center; }
  .result-n { font-size: 32px; font-weight: 800; color: var(--accent2); }
  .account-balance-post { margin-top: 12px; display: flex; flex-direction: column; align-items: center; gap: 4px; }
  .balance-value { font-size: 22px; font-weight: 700; }

  /* Budget month */
  .month-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
  .month-nav { display: flex; align-items: center; gap: 6px; }
  .month-nav input[type=month] { padding: 5px 10px; }
  .bm-summary { display: flex; gap: 16px; flex-wrap: wrap; }
  .bm-stat { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 18px; display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .bm-stat span { font-size: 12px; color: var(--text2); }
  .bm-stat strong { font-size: 18px; font-weight: 700; }
  .bm-stat strong.over { color: var(--danger); }
  .bm-table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
  .bm-table { width: 100%; border-collapse: collapse; }
  .bm-table th { padding: 9px 12px; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2); background: var(--surface2); }
  .bm-table td { padding: 9px 12px; border-top: 1px solid var(--border); font-size: 13px; }
  .bm-table tr:hover td { background: var(--surface2); }
  .cat-name-cell { font-weight: 500; margin-bottom: 4px; }
  .bm-bar-wrap { height: 4px; background: var(--surface2); border-radius: 999px; overflow: hidden; }
  .bm-bar { height: 100%; background: var(--accent); border-radius: 999px; transition: width 0.4s; }
  .bm-bar.over { background: var(--danger); }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .spent { color: var(--text2); }
  .over { color: var(--danger); }
</style>
