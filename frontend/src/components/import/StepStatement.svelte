<!--
  Step 1 — upload a statement and confirm which Actual account it goes into.
  Continue is blocked while the chosen account contradicts a confident
  recommendation (overridable) or would duplicate rows already in another account (not overridable).
-->
<script>
  import { createEventDispatcher } from 'svelte';
  import Icon from '../Icon.svelte';

  export let statement = null;     // StatementInfo from /parse
  export let rec = null;           // { suggestions, recommended, auto_select, remembered }
  export let accounts = [];        // Actual accounts (balance in cents)
  export let selectedId = '';
  export let total = 0;            // transactions in the statement
  export let loading = false;
  export let parseError = '';
  export let recLoading = false;
  export let locked = false;       // already imported: the account can't change without an undo
  export let blocked = true;       // out: can't continue
  export let seenBefore = [];      // earlier imports of this exact file (import history)

  const dispatch = createEventDispatcher();
  let dragover = false;
  let fileInput;
  let override = false;            // user insists on an account that contradicts the recommendation

  $: suggestions = rec?.suggestions ?? [];
  $: recommended = rec?.recommended;
  $: byId = new Map(accounts.map(a => [a.id, a]));
  // radio cards: suggested accounts (score > 0), recommended first; the rest go in a select
  $: cards = (() => {
    const s = suggestions.filter(x => x.score > 0 && byId.has(x.account_id) && !byId.get(x.account_id).closed);
    s.sort((a, b) => (b.account_id === recommended) - (a.account_id === recommended));
    const list = s.slice(0, 4).map(x => ({ id: x.account_id, name: x.name, reasons: x.reasons ?? [], rec: x.account_id === recommended }));
    if (selectedId && !list.find(c => c.id === selectedId) && byId.has(selectedId))
      list.push({ id: selectedId, name: byId.get(selectedId).name, reasons: [], rec: false });
    return list;
  })();
  $: others = accounts.filter(a => !a.closed && !cards.find(c => c.id === a.id));

  $: selected = suggestions.find(s => s.account_id === selectedId);
  $: selectedName = byId.get(selectedId)?.name ?? '';
  $: recName = suggestions.find(s => s.account_id === recommended)?.name ?? byId.get(recommended)?.name ?? '';
  $: mismatch = !!(rec?.auto_select && recommended && selectedId && selectedId !== recommended);
  $: holder = suggestions.find(s => s.account_id !== selectedId && (s.matched || 0) >= Math.max(3, 0.2 * total));
  $: conflict = !!(selectedId && holder && !(selected?.matched));
  $: if (!mismatch) override = false;
  $: blocked = !statement || !selectedId || conflict || (mismatch && !override);

  function pick(id) { override = false; dispatch('pick', id); }
  function money(c) { return c == null ? '' : (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }); }
  function stmtMoney(v) { return v == null ? '' : v.toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }); }
  function onDrop(e) { dragover = false; const f = e.dataTransfer.files[0]; if (f) dispatch('upload', f); }
  function fmtDay(d) { return d ? new Date(d).toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric' }) : ''; }
</script>

<input bind:this={fileInput} type="file" accept=".xls,.xlsx,.csv,.pdf" hidden
  on:change={(e) => { const f = e.target.files[0]; e.target.value = ''; if (f) dispatch('upload', f); }} />

{#if !statement}
  <h1>Import a bank statement</h1>
  <button class="dropzone" class:dragover disabled={loading}
    on:dragover|preventDefault={() => (dragover = true)}
    on:dragleave={() => (dragover = false)}
    on:drop|preventDefault={onDrop}
    on:click={() => fileInput.click()}>
    {#if loading}
      <span class="spinner lg"></span>
      <span class="dz-title">Reading statement…</span>
    {:else}
      <span class="dz-icon"><Icon name="upload" size={28} /></span>
      <span class="dz-title">Drop a statement here, or click to choose a file</span>
      <span class="dz-sub">UOB · DBS / POSB · OCBC — XLS, XLSX, CSV or PDF</span>
    {/if}
  </button>
  {#if parseError}<div class="banner error">{parseError}</div>{/if}
{:else}
  <h1>Which account is this statement for?</h1>

  <div class="stmt">
    <span class="stmt-icon"><Icon name="file" size={22} stroke={1.8} /></span>
    <div class="stmt-body">
      <div class="stmt-name">{statement.label}</div>
      <div class="muted">
        {#if statement.period_start}{fmtDay(statement.period_start)} → {fmtDay(statement.period_end)} · {/if}{total} transactions{#if statement.balance != null}{' · '}{statement.balance_is_owed ? 'owed' : 'closing balance'}&nbsp;<span class="mono" style="color:var(--text)">{stmtMoney(statement.balance)}</span>{/if}
      </div>
    </div>
    <button class="ghost sm" on:click={() => fileInput.click()} disabled={loading}>{loading ? 'Reading…' : 'Replace file'}</button>
  </div>

  <div class="accts">
    <div class="eyebrow">Import into</div>
    {#if recLoading && !cards.length}
      <div class="muted">Finding the right account…</div>
    {/if}
    {#each cards as c (c.id)}
      {@const on = c.id === selectedId}
      <button class="acct" disabled={locked && c.id !== selectedId} class:on class:bad={on && (conflict || (mismatch && !override))} aria-pressed={on} on:click={() => pick(c.id)}>
        <span class="radio"></span>
        <span class="acct-body">
          <span class="acct-name">{c.name}{#if c.rec}<span class="rec-tag">Recommended</span>{/if}</span>
          {#if c.reasons.length}<span class="muted acct-why">{c.reasons.join(' · ')}</span>{/if}
        </span>
        <span class="mono muted">{money(byId.get(c.id)?.balance)}</span>
      </button>
    {/each}
    {#if locked}
      <div class="muted">Already imported into {selectedName}. Undo the import on the Import step to move it.</div>
    {:else if others.length}
      <label class="other">
        <span>{cards.length ? 'Another account' : 'Choose an account'}</span>
        <select value="" on:change={(e) => { if (e.target.value) pick(e.target.value); e.target.value = ''; }}>
          <option value="">Select…</option>
          {#each others as a}<option value={a.id}>{a.name}{a.balance != null ? ` · ${money(a.balance)}` : ''}</option>{/each}
        </select>
      </label>
    {/if}
  </div>

  {#if seenBefore.length}
    {@const last = seenBefore[0]}
    <div class="banner warn" role="status">
      <span class="b-icon"><Icon name="info" size={20} /></span>
      <div class="b-body">
        <div><strong>You've uploaded this exact file before</strong> — {new Date(last.created * 1000).toLocaleString('en-SG', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })},
          {last.status === 'imported' ? `imported ${last.added} into ${last.account_name || 'Actual'}` : last.status === 'nothing_new' ? 'nothing new was imported' : last.status === 'undone' ? 'later undone' : last.status === 'failed' ? 'it failed' : 'not imported'}{seenBefore.length > 1 ? ` (${seenBefore.length} times in all)` : ''}.
          Rows already in Actual are skipped either way. <a href="#history">History</a></div>
      </div>
    </div>
  {/if}

  {#if conflict}
    <div class="banner warn" role="alert">
      <span class="b-icon"><Icon name="alert" size={20} /></span>
      <div class="b-body">
        <div><strong>{holder.matched} of these {total} transactions are already in {holder.name}.</strong> Importing into {selectedName} would duplicate them.</div>
        <div><button class="primary sm" on:click={() => pick(holder.account_id)}>Switch to {holder.name}</button></div>
      </div>
    </div>
  {:else if mismatch && !override}
    <div class="banner warn" role="alert">
      <span class="b-icon"><Icon name="alert" size={20} /></span>
      <div class="b-body">
        <div><strong>This looks like a {recName} statement, not {selectedName}.</strong> Importing here would put {total} transactions in the wrong account and throw off both balances.</div>
        <div class="b-actions">
          <button class="primary sm" on:click={() => pick(recommended)}>Switch to {recName}</button>
          <button class="linkish" on:click={() => (override = true)}>It really is {selectedName}</button>
        </div>
      </div>
    </div>
  {:else if !selectedId && !recLoading}
    <div class="muted">No confident match. Pick the account; it'll be remembered for this card or account number after import.</div>
  {/if}
{/if}

<div class="foot">
  <span></span>
  <button class="primary lg" disabled={blocked || loading} on:click={() => dispatch('continue')}>
    Continue to categories <Icon name="arrow" />
  </button>
</div>

<style>
  h1 { font-size: 26px; font-weight: 600; letter-spacing: -.01em; }
  .dropzone {
    flex-direction: column; gap: 10px; width: 100%; min-height: 280px;
    border: 2px dashed var(--border); border-radius: 16px; background: var(--surface); color: var(--text);
  }
  .dropzone:hover, .dropzone.dragover { border-color: var(--accent); background: #1b1a33; }
  .dz-icon { color: var(--accent); }
  .dz-title { font-size: 17px; font-weight: 600; }
  .dz-sub { font-size: 13px; color: var(--text2); }
  .stmt { display: flex; align-items: center; gap: 16px; padding: 16px 18px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); }
  .stmt-icon { width: 44px; height: 44px; border-radius: 10px; background: var(--surface2); display: flex; align-items: center; justify-content: center; color: #c9cdd6; flex-shrink: 0; }
  .stmt-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .stmt-name { font-size: 16px; font-weight: 600; }
  .accts { display: flex; flex-direction: column; gap: 10px; }
  .acct {
    justify-content: flex-start; gap: 16px; padding: 14px 18px; border-radius: 12px; width: 100%;
    border: 1.5px solid var(--border); background: var(--surface); color: var(--text); text-align: left;
  }
  .acct:hover { border-color: #3a404d; }
  .acct:disabled { opacity: .45; cursor: not-allowed; }
  .acct.on { border-color: var(--accent); background: #1b1a33; }
  .acct.bad { border-color: var(--warn); background: var(--warn-bg); }
  .radio { width: 18px; height: 18px; border-radius: 9px; border: 2px solid #4a5160; flex-shrink: 0; box-sizing: border-box; }
  .acct.on .radio { border: 5px solid var(--accent); }
  .acct.bad .radio { border-color: var(--warn); }
  .acct-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .acct-name { display: flex; align-items: center; gap: 10px; font-size: 15px; font-weight: 600; }
  .acct-why { font-size: 13px; font-weight: 400; }
  .rec-tag { font-size: 12px; font-weight: 600; color: var(--accent2); background: var(--ok-bg); padding: 1px 8px; border-radius: 6px; }
  .other { flex-direction: row; align-items: center; gap: 12px; font-size: 13px; }
  .other select { min-width: 240px; }
  .b-body { display: flex; flex-direction: column; gap: 12px; flex: 1; }
  .b-body strong { color: var(--warn); }
  .b-actions { display: flex; gap: 16px; align-items: center; }
  .foot { margin-top: auto; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; }
</style>
