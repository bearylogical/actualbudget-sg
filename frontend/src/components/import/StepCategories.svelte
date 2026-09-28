<!--
  Step 2 — check categories. Rows that need a decision (no category, a seed
  category Actual doesn't have, or a low-confidence AI guess) are highlighted and
  can be filtered to; everything else can be edited inline.
-->
<script>
  import { createEventDispatcher } from 'svelte';
  import Icon from '../Icon.svelte';

  export let transactions = [];
  export let categories = [];            // flat names (fallback list)
  export let actualCategoryGroups = [];
  export let actualCats = [];            // [{id, name, group}]
  export let actualBudgetLoaded = false;
  export let recategorising = false;
  export let catStats = null;
  export let includeCredits = true;

  const dispatch = createEventDispatcher();
  const UNCAT = 'Uncategorized';
  const SOURCE_LABEL = { actual: 'Actual rule', seed: 'Built-in rule', llm: 'AI guess', manual: 'You' };

  let filter = 'all';                    // 'all' | 'needs'
  let view = 'list';                     // 'list' | 'summary'
  let search = '';
  let editingId = null;
  let editingCategory = '';

  const GUESS_MIN = 0.8;                 // AI guesses below this confidence get a second look
  const isUncat = (t) => t.kind !== 'transfer' && (!t.category || t.category === UNCAT);
  const isGuess = (t) => t.source === 'llm' && (t.confidence ?? 0) < GUESS_MIN;
  // ack = looked at and accepted as-is (an uncategorised row keeps its #review tag for later)
  const needsOf = (t) => !t.ack && (t.unmapped || isUncat(t) || isGuess(t));

  $: rows = transactions.filter((t) => includeCredits || !t.is_credit);
  $: needs = rows.filter(needsOf);
  $: unmappedN = rows.filter((t) => t.unmapped).length;
  $: guesses = rows.filter((t) => !t.ack && !t.unmapped && isGuess(t));
  $: shown = (filter === 'needs' ? needs : rows)
    .filter((t) => !search || `${t.payee ?? ''} ${t.description}`.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => new Date(b.date) - new Date(a.date));

  $: spending = rows.filter((t) => !t.is_credit);
  $: totalSpend = spending.reduce((s, t) => s + t.amount, 0);
  $: byCat = Object.entries(spending.reduce((m, t) => ((m[t.category] = (m[t.category] || 0) + t.amount), m), {}))
    .map(([cat, total]) => ({ cat, total })).sort((a, b) => b.total - a.total);

  const isUncatName = (c) => !c || c === UNCAT;
  function startEdit(t) { editingId = t.id; editingCategory = t.category; }
  function saveEdit(t) {
    if (editingId !== t.id) return;
    const picked = editingCategory;
    editingId = null;
    if (picked === t.category) return;
    const hit = actualCats.find((c) => c.name === picked);
    // apply to every row with the same payee — that's what the learned rule will do too
    transactions = transactions.map((tx) =>
      tx.id === t.id || (tx.payee && tx.payee === t.payee && tx.is_credit === t.is_credit && tx.source !== 'manual')
        ? { ...tx, category: picked, category_id: hit?.id ?? null, source: 'manual', confidence: 1, unmapped: false,
            notes: (tx.notes || '').replace(/#llm|#review/g, '').trim() }
        : tx);
  }
  function accept(ids) {
    const set = new Set(ids);
    transactions = transactions.map((tx) => (set.has(tx.id) ? { ...tx, ack: true } : tx));
  }

  function fmtAmt(n) { return n.toLocaleString('en-SG', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function fmtDate(d) { return new Date(d).toLocaleDateString('en-SG', { day: '2-digit', month: 'short' }); }
  function catColor(name) {
    if (!name || name === UNCAT) return '#6b7280';
    let h = 0; for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 55% 62%)`;
  }
  function srcLabel(t) {
    if (t.source) return SOURCE_LABEL[t.source] + (t.source === 'llm' ? ` ${Math.round(t.confidence * 100)}%` : '');
    if (t.kind === 'transfer') return 'Transfer';
    if (t.kind === 'p2p') return 'PayNow/FAST';
    return '';
  }
</script>

<div class="head">
  <div class="head-l">
    <h1>Check categories</h1>
    <div class="muted">
      {rows.length} transactions · {rows.length - needs.length} categorised · {#if needs.length}<span class="warn-t">{needs.length} need a look</span>{:else}all set{/if}
      {#if recategorising}<span class="spinner" style="width:14px;height:14px;vertical-align:-2px;margin-left:6px"></span>{/if}
    </div>
  </div>
  <div class="head-r">
    {#if unmappedN}<button class="ghost sm" on:click={() => dispatch('openMapper')}>Map {unmappedN} category {unmappedN === 1 ? 'name' : 'names'} to Actual</button>{/if}
    {#if guesses.length}<button class="ghost sm" on:click={() => accept(guesses.map((t) => t.id))}><Icon name="check" size={16} /> Accept {guesses.length} AI {guesses.length === 1 ? 'guess' : 'guesses'}</button>{/if}
  </div>
</div>

<div class="bar">
  <button class="chip" class:on={view === 'list' && filter === 'all'} on:click={() => { view = 'list'; filter = 'all'; }}>All · {rows.length}</button>
  <button class="chip" class:on={view === 'list' && filter === 'needs'} on:click={() => { view = 'list'; filter = 'needs'; }}>Needs a look · {needs.length}</button>
  <button class="chip" class:on={view === 'summary'} on:click={() => (view = 'summary')}>By category</button>
  <input class="search" placeholder="Search" bind:value={search} aria-label="Search transactions" />
  <button class="ghost sm" on:click={() => dispatch('export', shown)} title="Download these rows as CSV"><Icon name="download" size={16} /> CSV</button>
</div>

{#if view === 'list'}
  <div class="table">
    <div class="tr th"><span>Date</span><span>Description</span><span>Category</span><span class="r">Amount</span></div>
    <div class="tbody">
      {#each shown as t (t.id)}
        {@const n = needsOf(t)}
        <div class="tr" class:needs={n}>
          <span class="mono muted">{fmtDate(t.date)}</span>
          <span class="desc">
            <span class="payee">{t.payee || t.description}</span>
            {#if t.payee && t.payee !== t.description}<span class="raw">{t.description}</span>{/if}
            {#if t.foreign_amount}<span class="raw">{t.foreign_currency} {t.foreign_amount}</span>{/if}
          </span>
          <span class="cat">
            {#if editingId === t.id}
              <!-- svelte-ignore a11y-autofocus -->
              <select bind:value={editingCategory} on:change={() => saveEdit(t)} on:blur={() => saveEdit(t)} autofocus>
                {#if actualBudgetLoaded && actualCats.length}
                  {#each actualCategoryGroups as g}
                    <optgroup label={g.name}>
                      {#each (g.categories || []).filter((c) => !c.hidden) as c}<option value={c.name}>{c.name}</option>{/each}
                    </optgroup>
                  {/each}
                  <option value={UNCAT}>{UNCAT}</option>
                {:else}
                  {#each categories as c}<option value={c}>{c}</option>{/each}
                {/if}
              </select>
            {:else}
              <button class="pill" class:pill-warn={n} style="--c:{catColor(t.category)}" on:click={() => startEdit(t)}
                title={t.unmapped ? 'Actual has no category with this name — map it, or pick another' : 'Change category'}>
                {t.kind === 'transfer' && isUncatName(t.category) ? 'Transfer' : isUncatName(t.category) ? 'Choose…' : t.category}{n && (isGuess(t) || t.unmapped) ? '?' : ''}
              </button>
              {#if n && t.unmapped}<button class="accept" on:click={() => dispatch('openMapper')}>Map</button>
              {:else if n && isGuess(t)}<button class="accept" on:click={() => accept([t.id])}>Accept</button>
              {:else if n}<button class="accept quiet" on:click={() => accept([t.id])} title="Import without a category; it stays tagged #review in Actual">Skip</button>{/if}
              {#if !n && srcLabel(t)}<span class="src">{srcLabel(t)}</span>{/if}
            {/if}
          </span>
          <span class="mono r" class:credit={t.is_credit}>{t.is_credit ? '+' : '−'}{fmtAmt(t.amount)}</span>
        </div>
      {:else}
        <div class="empty">{filter === 'needs' ? 'Every transaction has a category.' : 'No matching transactions.'}</div>
      {/each}
    </div>
  </div>
  {#if catStats}<div class="muted small">Categorised by {catStats.actual} Actual rules · {catStats.seed} built-in rules · {catStats.llm} AI · {catStats.manual ?? 0} by you</div>{/if}
{:else}
  <div class="summary">
    <div class="muted">SGD {fmtAmt(totalSpend)} spent across {spending.length} transactions</div>
    {#each byCat as { cat, total }}
      <div class="srow">
        <span class="sdot" style="background:{catColor(cat)}"></span>
        <span class="sname">{cat}</span>
        <span class="sbar"><span style="width:{(total / totalSpend) * 100}%;background:{catColor(cat)}"></span></span>
        <span class="mono spct muted">{((total / totalSpend) * 100).toFixed(0)}%</span>
        <span class="mono samt">{fmtAmt(total)}</span>
      </div>
    {/each}
  </div>
{/if}

<div class="foot">
  <button class="ghost lg" on:click={() => dispatch('back')}>Back</button>
  <div class="foot-r">
    {#if needs.length}
      <button class="linkish" on:click={() => dispatch('continue')}>Continue anyway ({needs.length} {needs.length === 1 ? 'needs' : 'need'} a look)</button>
    {/if}
    <button class="primary lg" disabled={needs.length > 0} on:click={() => dispatch('continue')}>Continue to import <Icon name="arrow" /></button>
  </div>
</div>

<style>
  h1 { font-size: 26px; font-weight: 600; letter-spacing: -.01em; }
  .head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; }
  .head-l { display: flex; flex-direction: column; gap: 4px; }
  .head-r { display: flex; gap: 8px; flex-wrap: wrap; }
  .warn-t { color: var(--warn); }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .search { margin-left: auto; width: 220px; height: 34px; padding: 0 12px; font-size: 13px; }
  .table { border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface); display: flex; flex-direction: column; min-height: 0; flex: 1; overflow: hidden; }
  .tbody { overflow-y: auto; flex: 1; min-height: 120px; }
  .tr { display: grid; grid-template-columns: 72px minmax(0, 1fr) minmax(220px, 300px) 110px; gap: 16px; align-items: center; padding: 8px 18px; border-top: 1px solid var(--border-soft); font-size: 14px; min-height: 48px; }
  .th { border-top: 0; min-height: 40px; font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--text3); }
  .tr.needs { background: #1d1910; }
  .tbody .tr:hover { background: var(--surface2); }
  .tr.needs:hover { background: #241f13; }
  .desc { min-width: 0; display: flex; flex-direction: column; }
  .payee { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .raw { font-size: 12px; color: var(--text3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cat { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .cat select { height: 32px; padding: 0 8px; font-size: 13px; width: 100%; }
  .pill { height: 28px; padding: 0 10px; border-radius: 7px; font-size: 13px; font-weight: 500;
    background: color-mix(in srgb, var(--c) 14%, transparent); color: var(--c); border: 1px solid color-mix(in srgb, var(--c) 30%, transparent);
    max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; line-height: 26px; }
  .pill-warn { background: #2e2411; color: var(--warn); border: 1px dashed #6b521f; }
  .accept { height: 28px; padding: 0 10px; border-radius: 7px; border: 1px solid var(--accent-fill); background: none; color: #b9b6ff; font-size: 13px; font-weight: 600; }
  .accept:hover { background: #1b1a33; }
  .accept.quiet { border-color: var(--border); color: var(--text2); font-weight: 500; }
  .src { font-size: 12px; color: var(--text3); white-space: nowrap; }
  .r { text-align: right; }
  .credit { color: var(--accent2); }
  .empty { padding: 28px 18px; border-top: 1px solid var(--border-soft); color: var(--accent2); }
  .small { font-size: 12px; }
  .summary { display: flex; flex-direction: column; gap: 10px; overflow-y: auto; flex: 1; min-height: 0; }
  .srow { display: grid; grid-template-columns: 10px 180px minmax(0, 1fr) 48px 110px; gap: 12px; align-items: center; font-size: 14px; }
  .sdot { width: 10px; height: 10px; border-radius: 5px; }
  .sbar { height: 6px; background: var(--surface2); border-radius: 3px; overflow: hidden; }
  .sbar span { display: block; height: 100%; border-radius: 3px; }
  .spct, .samt { text-align: right; }
  .foot { margin-top: auto; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; }
  .foot-r { display: flex; align-items: center; gap: 16px; }
  .foot-r .linkish { color: var(--text2); font-size: 14px; }
</style>
