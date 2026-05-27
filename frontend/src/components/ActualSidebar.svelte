<!--
  ActualSidebar — persistent Actual Budget connection panel.
  Stays connected across the whole session. Exposes connection state
  and loaded data to the parent via props/events.
-->
<script>
  import { onMount, createEventDispatcher } from 'svelte';

  const API = '/api';
  const STORAGE_KEY = 'budget-actual-conn';
  const SECRETS_KEY = 'budget-actual-secrets';
  const dispatch = createEventDispatcher();

  // ── Connection inputs ───────────────────────────────────────────────────────
  let serverURL = '';
  let password = '';
  let encryptionPassword = '';
  let useEncryption = false;
  let sameAsServerPassword = true; // reuse server password as encryption password

  // ── Actual state ────────────────────────────────────────────────────────────
  export let connected = false;         // true once init succeeds
  export let budgetLoaded = false;      // true once a budget is downloaded
  export let accounts = [];
  export let categoryGroups = [];
  export let payees = [];
  export let rules = [];
  export let selectedAccountId = '';

  // ── Budget month ────────────────────────────────────────────────────────────
  let budgetMonthData = null;
  let budgetMonth = currentYearMonth();
  let loadingMonth = false;

  // ── UI ──────────────────────────────────────────────────────────────────────
  let budgets = [];
  let selectedBudgetGroupId = '';
  let loadingConnect = false;
  let loadingBudget = false;
  let error = '';
  let section = 'connect'; // 'connect' | 'budget' | 'ready'

  // ── Persistence ─────────────────────────────────────────────────────────────
  onMount(() => {
    try {
      const s = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      if (s.serverURL) serverURL = s.serverURL;
      if (s.useEncryption) useEncryption = s.useEncryption;
      if (typeof s.sameAsServerPassword === 'boolean') sameAsServerPassword = s.sameAsServerPassword;
      if (s.selectedBudgetGroupId) selectedBudgetGroupId = s.selectedBudgetGroupId;
      if (s.selectedAccountId) selectedAccountId = s.selectedAccountId;
    } catch {}
    try {
      const sec = JSON.parse(sessionStorage.getItem(SECRETS_KEY) || '{}');
      if (sec.password) password = sec.password;
      if (sec.encryptionPassword) encryptionPassword = sec.encryptionPassword;
    } catch {}
  });

  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        serverURL, useEncryption, sameAsServerPassword, selectedBudgetGroupId, selectedAccountId
      }));
    } catch {}
  }

  function persistSecrets() {
    try {
      sessionStorage.setItem(SECRETS_KEY, JSON.stringify({ password, encryptionPassword }));
    } catch {}
  }

  function clearSecrets() {
    try { sessionStorage.removeItem(SECRETS_KEY); } catch {}
  }

  // ── Helpers ─────────────────────────────────────────────────────────────────
  function currentYearMonth() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  function fmtBalance(b) {
    if (b === null || b === undefined) return '—';
    return (b / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' });
  }

  $: selectedAccount = accounts.find(a => a.id === selectedAccountId);

  function notify() {
    dispatch('change', { connected, budgetLoaded, accounts, categoryGroups, payees, rules, selectedAccountId });
  }

  // ── API ─────────────────────────────────────────────────────────────────────
  async function connect() {
    if (!serverURL || !password) { error = 'Server URL and password required'; return; }
    loadingConnect = true; error = '';
    try {
      const res = await fetch(`${API}/actual/budgets`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ serverURL, password })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      budgets = data.budgets;
      connected = true;
      section = 'budget';
      persist();
      persistSecrets();
    } catch (e) { error = e.message; }
    finally { loadingConnect = false; }
  }

  async function loadBudget() {
    if (!selectedBudgetGroupId) { error = 'Select a budget'; return; }
    loadingBudget = true; error = '';
    try {
      const body = { serverURL, password, budgetId: selectedBudgetGroupId };
      if (useEncryption) {
        const enc = sameAsServerPassword ? password : encryptionPassword;
        if (enc) body.encryptionPassword = enc;
      }
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
      budgetLoaded = true;
      section = 'ready';
      persist();
      persistSecrets();
      notify();
      fetchBudgetMonth();
    } catch (e) { error = e.message; }
    finally { loadingBudget = false; }
  }

  async function refreshAccounts() {
    try {
      const res = await fetch(`${API}/actual/accounts`);
      if (res.ok) { const d = await res.json(); accounts = d.accounts; notify(); }
    } catch {}
  }

  async function fetchBudgetMonth() {
    loadingMonth = true; budgetMonthData = null;
    try {
      const res = await fetch(`${API}/actual/budget-month/${budgetMonth}`);
      if (res.ok) budgetMonthData = await res.json();
    } catch {}
    finally { loadingMonth = false; }
  }

  async function disconnect() {
    await fetch(`${API}/actual/reset`, { method: 'POST' }).catch(() => {});
    connected = false; budgetLoaded = false;
    budgets = []; accounts = []; categoryGroups = []; payees = []; rules = [];
    selectedBudgetGroupId = ''; selectedAccountId = '';
    password = ''; encryptionPassword = '';
    budgetMonthData = null; section = 'connect'; error = '';
    clearSecrets();
    notify();
  }

  $: if (selectedAccountId) persist();

  // Budget month summary rows
  $: bmRows = (() => {
    if (!budgetMonthData?.budget?.categoryGroups) return [];
    return budgetMonthData.budget.categoryGroups
      .flatMap(g => (g.categories || []).map(c => ({
        name: c.name, group: g.name,
        budgeted: c.budgeted ?? 0,
        spent: c.spent ?? 0,
        balance: c.balance ?? 0,
      })))
      .filter(r => r.budgeted !== 0 || r.spent !== 0)
      .sort((a, b) => a.spent - b.spent); // most spent first (spent is negative)
  })();

  $: bmTotalBudgeted = bmRows.reduce((s, r) => s + r.budgeted, 0);
  $: bmTotalSpent = bmRows.reduce((s, r) => s + r.spent, 0);
  $: bmRemaining = bmTotalBudgeted + bmTotalSpent;

  function prevMonth() {
    const [y, m] = budgetMonth.split('-').map(Number);
    const d = new Date(y, m - 2);
    budgetMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    fetchBudgetMonth();
  }
  function nextMonth() {
    const [y, m] = budgetMonth.split('-').map(Number);
    const d = new Date(y, m);
    budgetMonth = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    fetchBudgetMonth();
  }
</script>

<aside class="sidebar">
  <div class="sidebar-header">
    <span class="sidebar-logo">⚡ Actual</span>
    {#if connected}
      <button class="ghost icon-btn" title="Disconnect" on:click={disconnect}>✕</button>
    {/if}
  </div>

  {#if error}
    <div class="error-msg sb-error">{error} <button on:click={() => error = ''}>✕</button></div>
  {/if}

  <!-- ── NOT CONNECTED ── -->
  {#if section === 'connect'}
    <div class="sb-form">
      <p class="sb-hint">Connect to your Actual Budget server to import statements and view your budget.</p>
      <label>Server URL
        <input bind:value={serverURL} placeholder="http://192.168.1.x:5006"
          on:keydown={e => e.key === 'Enter' && connect()} />
      </label>
      <label>Password
        <input type="password" bind:value={password} placeholder="Actual password"
          on:keydown={e => e.key === 'Enter' && connect()} />
      </label>
      <label class="row-label">
        <input type="checkbox" bind:checked={useEncryption} />
        End-to-end encryption
      </label>
      {#if useEncryption}
        <label class="row-label">
          <input type="checkbox" bind:checked={sameAsServerPassword} />
          Use server password for encryption
        </label>
        {#if !sameAsServerPassword}
          <label>Encryption Password
            <input type="password" bind:value={encryptionPassword} />
          </label>
        {/if}
      {/if}
      <button class="primary" on:click={connect} disabled={loadingConnect}>
        {#if loadingConnect}<span class="spinner"></span>{:else}Connect{/if}
      </button>
    </div>

  <!-- ── SELECT BUDGET ── -->
  {:else if section === 'budget'}
    <div class="sb-form">
      <p class="sb-hint">Select a budget file to load.</p>
      {#each budgets as b}
        <button class="budget-pick" class:selected={selectedBudgetGroupId === b.groupId}
          on:click={() => selectedBudgetGroupId = b.groupId}>
          <span class="bp-icon">📒</span>
          <span class="bp-name">{b.name}</span>
          <span class="bp-state">{b.state === 'remote' ? '☁' : '💾'}</span>
        </button>
      {/each}
      {#if !budgets.length}<p class="sb-hint">No budgets found on this server.</p>{/if}
      <button class="primary" on:click={loadBudget} disabled={loadingBudget || !selectedBudgetGroupId}>
        {#if loadingBudget}<span class="spinner"></span>{:else}Load Budget{/if}
      </button>
    </div>

  <!-- ── CONNECTED & READY ── -->
  {:else if section === 'ready'}
    <!-- Account selector -->
    <div class="sb-section">
      <div class="sb-section-label">Import Account</div>
      <select bind:value={selectedAccountId} on:change={() => persist()}>
        <option value="">— select account —</option>
        {#each accounts.filter(a => !a.closed) as acct}
          <option value={acct.id}>
            {acct.name}{acct.balance !== null ? ` · ${fmtBalance(acct.balance)}` : ''}
          </option>
        {/each}
      </select>
    </div>

    <!-- Budget month -->
    <div class="sb-section bm-section">
      <div class="bm-nav">
        <button class="ghost icon-btn" on:click={prevMonth}>‹</button>
        <span class="bm-month">{budgetMonth}</span>
        <button class="ghost icon-btn" on:click={nextMonth}>›</button>
        <button class="ghost icon-btn" on:click={fetchBudgetMonth} title="Refresh">↻</button>
      </div>

      {#if loadingMonth}
        <div class="bm-loading"><span class="spinner"></span></div>
      {:else if budgetMonthData}
        <div class="bm-totals">
          <div class="bm-total-row">
            <span>Budgeted</span><strong>{fmtBalance(bmTotalBudgeted)}</strong>
          </div>
          <div class="bm-total-row">
            <span>Spent</span><strong>{fmtBalance(Math.abs(bmTotalSpent))}</strong>
          </div>
          <div class="bm-total-row" class:over={bmRemaining < 0}>
            <span>Remaining</span><strong>{fmtBalance(bmRemaining)}</strong>
          </div>
        </div>

        <div class="bm-cats">
          {#each bmRows.slice(0, 10) as row}
            {@const pct = row.budgeted ? Math.min(100, Math.abs(row.spent) / row.budgeted * 100) : 0}
            {@const over = (row.budgeted + row.spent) < 0}
            <div class="bm-cat">
              <div class="bm-cat-top">
                <span class="bm-cat-name">{row.name}</span>
                <span class="bm-cat-spent" class:over>{fmtBalance(Math.abs(row.spent))}</span>
              </div>
              <div class="bm-bar-bg">
                <div class="bm-bar-fill" class:over style="width:{pct}%"></div>
              </div>
            </div>
          {/each}
          {#if bmRows.length > 10}
            <p class="sb-hint">+{bmRows.length - 10} more categories</p>
          {/if}
        </div>
      {:else}
        <p class="sb-hint">No budget data for this month.</p>
      {/if}
    </div>
  {/if}
</aside>

<style>
  .sidebar {
    width: var(--sidebar-w);
    min-width: var(--sidebar-w);
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow-y: auto;
  }

  .sidebar-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px; border-bottom: 1px solid var(--border);
    position: sticky; top: 0; background: var(--surface); z-index: 2;
  }
  .sidebar-logo { font-size: 15px; font-weight: 700; color: var(--accent); }

  .sb-error { margin: 10px; font-size: 12px; display: flex; justify-content: space-between; align-items: center; }
  .sb-error button { background: none; border: none; color: var(--danger); padding: 0 4px; font-size: 14px; }

  .sb-form { display: flex; flex-direction: column; gap: 12px; padding: 14px; }
  .sb-hint { font-size: 12px; color: var(--text2); line-height: 1.5; }

  .budget-pick {
    display: flex; align-items: center; gap: 8px; padding: 9px 12px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; text-align: left; color: var(--text); width: 100%;
    transition: all 0.15s;
  }
  .budget-pick:hover, .budget-pick.selected { border-color: var(--accent); background: #6c63ff11; }
  .bp-icon { font-size: 16px; }
  .bp-name { flex: 1; font-weight: 500; font-size: 13px; }
  .bp-state { color: var(--text2); font-size: 12px; }

  .sb-section { padding: 14px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 10px; }
  .sb-section-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text2); font-weight: 600; }
  .sb-section select { width: 100%; font-size: 13px; }

  .bm-section { flex: 1; }
  .bm-nav { display: flex; align-items: center; gap: 4px; }
  .bm-month { flex: 1; text-align: center; font-size: 13px; font-weight: 600; }
  .bm-loading { display: flex; justify-content: center; padding: 16px; }

  .bm-totals { background: var(--surface2); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; }
  .bm-total-row { display: flex; justify-content: space-between; font-size: 12px; }
  .bm-total-row.over strong { color: var(--danger); }
  .bm-total-row strong { font-weight: 600; }

  .bm-cats { display: flex; flex-direction: column; gap: 8px; }
  .bm-cat { display: flex; flex-direction: column; gap: 3px; }
  .bm-cat-top { display: flex; justify-content: space-between; font-size: 12px; }
  .bm-cat-name { color: var(--text2); }
  .bm-cat-spent { font-weight: 500; }
  .bm-cat-spent.over { color: var(--danger); }
  .bm-bar-bg { height: 3px; background: var(--surface3); border-radius: 999px; overflow: hidden; }
  .bm-bar-fill { height: 100%; background: var(--accent2); border-radius: 999px; transition: width 0.3s; }
  .bm-bar-fill.over { background: var(--danger); }
</style>
