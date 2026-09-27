<!--
  ReconcilePanel — does Actual match the bank? Explains the gap and queues fixes
  (duplicates, missing rows, opening balance) into the review queue.
-->
<script>
  import { createEventDispatcher } from 'svelte';
  import { readJson } from '../lib/http.js';
  export let accountId = '';
  export let accountName = '';
  export let statement = null;
  export let transactions = [];

  const API = '/api';
  const dispatch = createEventDispatcher();
  let balance = '';           // optional: today's balance / amount owed, overrides the statement
  let running = false;
  let res = null;
  let error = '';

  $: isCard = statement?.kind === 'credit_card';
  function money(c) { return c == null ? '—' : (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }); }

  async function run(useLLM = true) {
    running = true; error = ''; res = null;
    try {
      const body = { account_id: accountId, statement, transactions, use_llm: useLLM };
      if (balance !== '') body.balance = isCard ? -Math.abs(Number(balance)) : Number(balance);
      res = await readJson(await fetch(`${API}/reconcile`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }));
      if (res.queued?.length) dispatch('queued', res.queued.length);
    } catch (e) { error = e.message; }
    finally { running = false; }
  }
  $: r = res?.report;
</script>

<div class="rec">
  <div class="line">
    <strong>⚖ Reconcile {accountName}</strong>
    <label class="muted">{isCard ? 'Owed today (optional)' : 'Bank balance today (optional)'}
      <input type="number" step="0.01" bind:value={balance} placeholder={statement?.balance ?? ''} /></label>
    <button class="primary small" on:click={() => run(true)} disabled={running || !accountId}>{running ? 'Checking…' : 'Check balance'}</button>
    <button class="ghost small" on:click={() => run(false)} disabled={running || !accountId} title="Rules only, no AI">Rules only</button>
  </div>
  {#if error}<div class="error-msg">{error}</div>{/if}
  {#if r}
    <div class="figures">
      <div><span>Bank</span><strong>{money(r.expected_balance)}</strong></div>
      <div><span>Actual</span><strong>{money(r.actual_balance)}</strong></div>
      <div class:ok={r.reconciled} class:bad={!r.reconciled}><span>Gap</span><strong>{r.reconciled ? '✓ 0.00' : money(r.gap)}</strong></div>
      <div><span>As of</span><strong>{r.as_of}</strong></div>
    </div>
    {#if !r.balance_reliable}<div class="muted">⚠ {r.basis}</div>{/if}
    <div class="muted">
      {r.matched} matched · {r.missing.length} missing · {r.extra.length} extra · {r.duplicates.length} duplicate(s) · {r.transfers.length} unlinked transfer(s)
      {#if r.running_balance.first_divergence} · balances first diverge on <strong>{r.running_balance.first_divergence.date}</strong>{/if}
      {#if r.running_balance.constant_offset != null} · constant offset {money(r.running_balance.constant_offset)} = missing opening balance{/if}
    </div>
    {#if res.explanation}<div class="expl">🤖 {res.explanation}</div>{/if}
    {#if res.queued?.length}
      <div class="line">
        <span>{res.queued.length} fix(es) queued for your approval.</span>
        <button class="primary small" on:click={() => dispatch('openReview')}>Open review</button>
      </div>
    {:else if !r.reconciled}
      {#each r.recommendation as p}<div class="muted">• {p.title} — {p.why}</div>{/each}
    {/if}
  {/if}
</div>

<style>
  .rec { margin: 10px 20px 0; padding: 10px 14px; border-radius: 10px; background: var(--surface); border: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; font-size: 13px; }
  .line { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  label { display: flex; gap: 6px; align-items: center; }
  input { width: 130px; }
  .figures { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
  .figures div { background: var(--surface2); border-radius: 8px; padding: 6px 10px; display: flex; flex-direction: column; }
  .figures span { font-size: 11px; color: var(--text2); }
  .figures strong { font-variant-numeric: tabular-nums; }
  .figures .ok strong { color: var(--accent2); }
  .figures .bad strong { color: var(--warn); }
  .expl { background: var(--surface2); border-radius: 8px; padding: 8px 10px; white-space: pre-wrap; }
  .muted { color: var(--text2); font-size: 12px; }
  .small { font-size: 12px; padding: 4px 10px; }
</style>
