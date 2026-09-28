<!--
  ActualSidebar — Actual Budget connection card (connect → pick budget).
  Shown full-screen until a budget is loaded, then stays mounted but hidden so the
  connection state survives. The import account is chosen per statement in the
  import flow, so it is no longer remembered here.
-->
<script>
  import { onMount, createEventDispatcher } from 'svelte';
  import { readJson } from '../lib/http.js';

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
        serverURL, useEncryption, sameAsServerPassword, selectedBudgetGroupId
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
      const data = await readJson(res);
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
      const data = await readJson(res);
      accounts = data.accounts;
      categoryGroups = data.categoryGroups;
      payees = data.payees ?? [];
      rules = data.rules ?? [];
      budgetLoaded = true;
      section = 'ready';
      persist();
      persistSecrets();
      notify();
    } catch (e) { error = e.message; }
    finally { loadingBudget = false; }
  }

  async function refreshAccounts() {
    try {
      const res = await fetch(`${API}/actual/accounts`);
      if (res.ok) { const d = await res.json(); accounts = d.accounts; notify(); }
    } catch {}
  }

  export async function disconnect() {
    await fetch(`${API}/actual/reset`, { method: 'POST' }).catch(() => {});
    connected = false; budgetLoaded = false;
    budgets = []; accounts = []; categoryGroups = []; payees = []; rules = [];
    selectedBudgetGroupId = ''; selectedAccountId = '';
    password = ''; encryptionPassword = '';
    section = 'connect'; error = '';
    clearSecrets();
    notify();
  }

</script>

<aside class="sidebar">
  <div class="sidebar-header">
    <span class="sidebar-logo">Connect to Actual Budget</span>
    {#if connected}
      <button class="ghost sm" title="Disconnect — also stops scheduled imports until you load a budget again" on:click={disconnect}>Disconnect</button>
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
                    <span class="bp-name">{b.name}</span>
          <span class="bp-state">{b.state === 'remote' ? 'on server' : 'local'}</span>
        </button>
      {/each}
      {#if !budgets.length}<p class="sb-hint">No budgets found on this server.</p>{/if}
      <button class="primary" on:click={loadBudget} disabled={loadingBudget || !selectedBudgetGroupId}>
        {#if loadingBudget}<span class="spinner"></span>{:else}Load Budget{/if}
      </button>
    </div>

  {/if}
</aside>

<style>
  .sidebar {
    width: 100%; max-width: 440px;
    background: var(--panel); border: 1px solid var(--border-soft); border-radius: 16px;
    display: flex; flex-direction: column;
  }
  .sidebar-header { display: flex; align-items: center; justify-content: space-between; padding: 20px 22px 4px; }
  .sidebar-logo { font-size: 20px; font-weight: 600; }
  .sb-error { margin: 12px 22px 0; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
  .sb-error button { background: none; border: none; color: var(--danger); padding: 0 4px; font-size: 14px; }
  .sb-form { display: flex; flex-direction: column; gap: 14px; padding: 16px 22px 22px; }
  .sb-hint { font-size: 14px; color: var(--text2); line-height: 1.5; }
  .budget-pick {
    display: flex; align-items: center; gap: 10px; padding: 12px 14px; justify-content: flex-start;
    background: var(--surface); border: 1.5px solid var(--border);
    border-radius: 10px; text-align: left; color: var(--text); width: 100%;
  }
  .budget-pick:hover { border-color: #3a404d; }
  .budget-pick.selected { border-color: var(--accent); background: #1b1a33; }
  .bp-name { flex: 1; font-weight: 500; font-size: 14px; }
  .bp-state { color: var(--text2); font-size: 12px; }
</style>
