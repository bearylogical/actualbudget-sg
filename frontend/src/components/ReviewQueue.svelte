<!--
  ReviewQueue — everything that would change Actual because of a possible duplicate,
  an unlinked transfer or a reconciliation fix waits here for approval.
  AI verdicts are advisory; your decisions are remembered and teach the patterns.
-->
<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import { readJson } from '../lib/http.js';

  const API = '/api';
  const dispatch = createEventDispatcher();

  let tab = 'pending';          // pending | history | memory
  let items = [];
  let memory = [];
  let learnAfter = 2;
  let loading = false;
  let busy = {};                // item id → true while applying
  let error = '';
  let note = '';

  const KIND = {
    import_duplicate: 'Possible duplicate at import',
    existing_duplicate: 'Duplicate already in Actual',
    transfer_pair: 'Unlinked transfer',
    reconcile_fix: 'Reconciliation fix',
  };
  const ACTIONS = {
    import_duplicate: [['skip', 'Same transaction — skip'], ['import', 'Different — import it']],
    existing_duplicate: [['delete_a', 'Delete first'], ['delete_b', 'Delete second'], ['keep_both', 'Keep both']],
    transfer_pair: [['link', 'Link as transfer'], ['keep_separate', 'Keep separate']],
    reconcile_fix: [['apply', 'Apply fix'], ['reject', 'Reject']],
  };
  const VERDICT_TO = {
    import_duplicate: { duplicate: 'skip', not_duplicate: 'import' },
    transfer_pair: { transfer: 'link', not_transfer: 'keep_separate' },
    existing_duplicate: { not_duplicate: 'keep_both' },
  };

  function money(c) { return (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }); }

  async function load() {
    loading = true; error = '';
    try {
      if (tab === 'memory') {
        const d = await readJson(await fetch(`${API}/review/memory`));
        memory = d.memory; learnAfter = d.learn_after;
      } else {
        const status = tab === 'pending' ? 'pending' : '';
        const d = await readJson(await fetch(`${API}/review?status=${status}`));
        items = tab === 'pending' ? d.items : d.items.filter(i => i.status !== 'pending');
        dispatch('counts', d.counts);
      }
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }
  onMount(load);

  async function post(path, body) {
    return readJson(await fetch(`${API}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    }));
  }

  async function decide(it, decision) {
    busy = { ...busy, [it.id]: true }; error = '';
    try {
      const r = await post(`/review/${it.id}/decide`, { decision });
      if (r.applied && !r.applied.ok) error = `Applied with errors: ${r.applied.errors.join('; ')}`;
      dispatch('changed');
      await load();
    } catch (e) { error = e.message; }
    finally { busy = { ...busy, [it.id]: false }; }
  }

  async function scan() {
    loading = true; note = ''; error = '';
    try {
      const r = await post('/review/scan', { days: 120 });
      note = `Scan: ${r.queued} new item(s)` + (r.auto_applied ? `, ${r.auto_applied} resolved from what you taught it` : '');
      dispatch('changed');
    } catch (e) { error = e.message; }
    await load();
  }
  async function advise() {
    loading = true; note = ''; error = '';
    try { const r = await post('/review/advise', {}); note = `AI reviewed ${r.advised} item(s)`; }
    catch (e) { error = e.message; }
    await load();
  }
  async function acceptAI() {
    loading = true; note = ''; error = '';
    try {
      const r = await post('/review/accept-ai', { min_confidence: 0.8 });
      note = `Applied ${r.applied} AI suggestion(s) with ≥80% confidence (deletions always need you)`;
      dispatch('changed');
    } catch (e) { error = e.message; }
    await load();
  }
  async function forget(key) {
    const d = await post('/review/memory/forget', { key });
    memory = d.memory;
  }
  async function dismiss(it) { await post(`/review/${it.id}/dismiss`); await load(); }

  function rows(it) {
    const p = it.payload;
    if (it.kind === 'import_duplicate') {
      const t = p.incoming;
      return [{ tag: 'Incoming', date: t.date, amount: (t.is_credit ? 1 : -1) * Math.round(t.amount * 100), payee: t.payee, text: t.description, acct: p.account_name }];
    }
    if (p.a && p.b) return [{ tag: 'A', ...p.a, text: p.a.imported_payee, acct: p.a.account_name }, { tag: 'B', ...p.b, text: p.b.imported_payee, acct: p.b.account_name }];
    return [];
  }
  $: aiCount = items.filter(i => i.llm).length;
</script>

<!-- svelte-ignore a11y-no-static-element-interactions a11y-click-events-have-key-events -->
<div class="overlay" on:click|self={() => dispatch('close')}>
  <div class="modal">
    <div class="head">
      <h3>🧾 Review</h3>
      <div class="tabs">
        {#each [['pending', 'Pending'], ['history', 'History'], ['memory', 'What it learned']] as [k, l]}
          <button class="tab" class:active={tab === k} on:click={() => { tab = k; load(); }}>{l}</button>
        {/each}
      </div>
      <button class="ghost icon-btn" on:click={() => dispatch('close')}>✕</button>
    </div>

    {#if tab === 'pending'}
      <div class="bar">
        <button class="ghost small" on:click={scan} disabled={loading}>🔎 Scan Actual (120 days)</button>
        <button class="ghost small" on:click={advise} disabled={loading || !items.length}>🤖 Ask AI</button>
        <button class="ghost small" on:click={acceptAI} disabled={loading || !aiCount}>✓ Accept AI ≥80%</button>
        {#if loading}<span class="spinner"></span>{/if}
        <span class="muted">{note}</span>
      </div>
    {/if}
    {#if error}<div class="error-msg">{error}</div>{/if}

    <div class="list">
      {#if tab === 'memory'}
        <p class="muted">A pattern resolves new items on its own after you've made the same decision {learnAfter}×.
          Deletions and reconciliation fixes are never automated.</p>
        {#each memory as m}
          <div class="mem">
            <span><strong>{m.label}</strong> → {m.decision}</span>
            <span class="muted">×{m.count}{m.count >= learnAfter ? ' · active' : ''}</span>
            <button class="ghost small" on:click={() => forget(m.key)}>Forget</button>
          </div>
        {:else}<p class="muted">Nothing learned yet.</p>{/each}
      {:else}
        {#each items as it (it.id)}
          <div class="item" class:done={it.status !== 'pending'}>
            <div class="item-head">
              <span class="kind">{KIND[it.kind]}</span>
              {#if it.kind === 'reconcile_fix'}<strong>{it.payload.title}</strong>{/if}
              <span class="muted">{it.payload.reason || it.payload.why || ''}</span>
              {#if it.status !== 'pending'}<span class="st">{it.status}{it.decision ? ` · ${it.decision}` : ''}{it.decided_by ? ` · ${it.decided_by}` : ''}</span>{/if}
            </div>
            {#each rows(it) as r}
              <div class="row">
                <span class="tag">{r.tag}</span><span>{r.date}</span>
                <span class="amt" class:pos={r.amount > 0}>{money(r.amount)}</span>
                <span class="payee">{r.payee ?? '—'}</span><span class="muted acct">{r.acct ?? ''}</span>
                <span class="muted text">{r.text ?? ''}</span>
              </div>
            {/each}
            {#if it.kind === 'reconcile_fix'}
              <div class="muted">Effect on balance: {money(it.payload.effect)} · gap was {money(it.payload.gap ?? 0)} · via {it.payload.source}</div>
              {#each it.payload.actions as a}
                <div class="act"><span class="tag">{a.type}</span>
                  <span class="muted">{a.type === 'import' ? `${a.transactions.length} row(s): ${a.transactions.map(t => `${t.date} ${t.payee} ${t.amount}`).join(', ')}`
                    : a.type === 'delete' ? `${a.ids.length} transaction(s)` : a.type === 'adjust' ? `${money(a.amount_cents)} on ${a.date}` : 'link transfer'}</span></div>
              {/each}
            {/if}
            {#if it.llm}
              <div class="ai"><span class="ai-badge">AI: {it.llm.verdict} {Math.round(it.llm.confidence * 100)}%</span> {it.llm.reason}</div>
            {/if}
            {#if it.status === 'pending'}
              <div class="actions">
                {#each ACTIONS[it.kind] as [d, label]}
                  <button class:primary={VERDICT_TO[it.kind]?.[it.llm?.verdict] === d} class:ghost={VERDICT_TO[it.kind]?.[it.llm?.verdict] !== d}
                    class="small" disabled={busy[it.id]} on:click={() => decide(it, d)}>{label}</button>
                {/each}
                <button class="ghost small" on:click={() => dismiss(it)}>Later</button>
              </div>
            {/if}
          </div>
        {:else}
          <p class="muted">{tab === 'pending' ? 'Nothing waiting for review 🎉' : 'No history yet.'}</p>
        {/each}
      {/if}
    </div>
  </div>
</div>

<style>
  .overlay { position: fixed; inset: 0; background: #00000088; display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; width: 900px; max-width: 96vw; max-height: 90vh; display: flex; flex-direction: column; gap: 10px; padding: 18px; }
  .head { display: flex; align-items: center; gap: 12px; }
  .head h3 { font-size: 17px; font-weight: 700; }
  .tabs { display: flex; gap: 2px; flex: 1; }
  .tab { background: transparent; border: none; color: var(--text2); padding: 5px 12px; border-radius: 6px; font-size: 13px; }
  .tab.active { background: var(--surface2); color: var(--accent); }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .list { overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
  .item { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
  .item.done { opacity: .7; }
  .item-head { display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
  .kind { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--accent); }
  .st { font-size: 11px; color: var(--text2); margin-left: auto; }
  .row { display: grid; grid-template-columns: 70px 90px 110px 170px 130px 1fr; gap: 8px; align-items: baseline; }
  .tag { font-size: 11px; color: var(--text2); }
  .act { display: flex; gap: 10px; align-items: baseline; }
  .act .tag { min-width: 50px; text-transform: uppercase; letter-spacing: .04em; }
  .amt { font-variant-numeric: tabular-nums; text-align: right; }
  .amt.pos { color: var(--accent2); }
  .text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
  .ai { font-size: 12px; color: var(--text2); }
  .ai-badge { background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn); border-radius: 999px; padding: 1px 8px; font-size: 11px; margin-right: 6px; }
  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  .small { font-size: 12px; padding: 4px 10px; }
  .muted { color: var(--text2); font-size: 12px; }
  .mem { display: flex; gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .mem span:first-child { flex: 1; }
</style>
