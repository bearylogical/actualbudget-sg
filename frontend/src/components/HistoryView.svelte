<!--
  History — every statement file uploaded in the web UI or dropped in the watch folder:
  what it was, where it went, what happened, and the original file. Failed watch-folder
  files show the stage and error; an import that added rows can be undone from here.
-->
<script>
  import { onMount } from 'svelte';
  import { readJson } from '../lib/http.js';
  import Icon from './Icon.svelte';

  const API = '/api';
  let rows = [];
  let summary = null;
  let loading = true;
  let error = '';
  let status = '';            // '' = all
  let source = '';
  let open = {};              // id → detail (loaded on expand)
  let busy = {};
  let confirmUndo = '';

  const STATUS = {
    imported:    { label: 'Imported',     icon: 'check', tone: 'good' },
    nothing_new: { label: 'Nothing new',  icon: 'check', tone: 'neutral' },
    failed:      { label: 'Failed',       icon: 'alert', tone: 'bad' },
    undone:      { label: 'Undone',       icon: 'undo',  tone: 'neutral' },
    parsed:      { label: 'Not imported', icon: 'clock', tone: 'warn' },
  };
  const STAGE = { parse: 'reading the file', connect: 'connecting to Actual', account: 'choosing the account',
                  import: 'importing', checks: 'post-import checks' };
  const FILTERS = [['', 'All'], ['imported', 'Imported'], ['nothing_new', 'Nothing new'], ['failed', 'Failed'],
                   ['parsed', 'Not imported'], ['undone', 'Undone']];

  async function load() {
    loading = true; error = '';
    try {
      const qs = new URLSearchParams({ limit: '300', status, source });
      const d = await readJson(await fetch(`${API}/imports?${qs}`));
      rows = d.imports; summary = d.summary;
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }
  onMount(load);

  async function toggle(r) {
    if (open[r.id]) { const { [r.id]: _, ...rest } = open; open = rest; return; }
    open = { ...open, [r.id]: { loading: true } };
    try { open = { ...open, [r.id]: await readJson(await fetch(`${API}/imports/${r.id}`)) }; }
    catch (e) { open = { ...open, [r.id]: { loadError: e.message } }; }
  }

  async function undo(r) {
    busy = { ...busy, [r.id]: true }; error = ''; confirmUndo = '';
    try {
      const d = await readJson(await fetch(`${API}/imports/${r.id}/undo`, { method: 'POST' }));
      open = { ...open, [r.id]: d.import };
      rows = rows.map((x) => (x.id === r.id ? { ...x, status: 'undone' } : x));
    } catch (e) { error = e.message; }
    finally { busy = { ...busy, [r.id]: false }; }
  }

  const when = (ts) => new Date(ts * 1000).toLocaleString('en-SG', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  const ago = (ts) => {
    const s = Date.now() / 1000 - ts;
    if (s < 90) return 'just now';
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    if (s < 129600) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  };
  const day = (d) => (d ? new Date(d).toLocaleDateString('en-SG', { day: 'numeric', month: 'short' }) : '');
  const money = (c) => (c == null ? '—' : (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }));
  const kb = (n) => (n == null ? '' : n < 1024 ? `${n} B` : `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`);
  const period = (r) => (r.period_start || r.period_end ? `${day(r.period_start)} – ${day(r.period_end)}` : '');

  $: counts = summary?.counts || {};
  $: total = Object.values(counts).reduce((a, b) => a + b, 0);
</script>

<div class="page">
  <div class="view-head">
    <div>
      <h1>History</h1>
      <p class="muted sub">Every statement uploaded here or dropped in the watch folder.</p>
    </div>
    <div class="view-actions">
      <button class="ghost icon-btn" on:click={load} disabled={loading} aria-label="Refresh"><Icon name="refresh" size={16} /></button>
    </div>
  </div>
    <div class="tiles">
      <div class="tile"><span class="t-label">Files</span><span class="t-value">{total}</span><span class="t-sub">uploaded or dropped</span></div>
      <div class="tile ok"><span class="t-label">Imported</span><span class="t-value">{counts.imported || 0}</span><span class="t-sub">{counts.nothing_new ? `+ ${counts.nothing_new} with nothing new` : 'added rows to Actual'}</span></div>
      <div class="tile" class:bad={counts.failed}><span class="t-label">Failed</span><span class="t-value">{counts.failed || 0}</span><span class="t-sub">{counts.failed ? 'see the error below' : 'none'}</span></div>
      <div class="tile" class:warn={counts.parsed}><span class="t-label">Not imported</span><span class="t-value">{counts.parsed || 0}</span><span class="t-sub">uploaded, never imported</span></div>
      <div class="tile"><span class="t-label">Last import</span><span class="t-value sm">{summary?.last ? ago(summary.last.created) : '—'}</span><span class="t-sub ellip">{summary?.last?.filename || ''}</span></div>
    </div>

    <div class="filters">
      <div class="chips" role="group" aria-label="Status">
        {#each FILTERS as [k, l]}
          <button class="chip" class:on={status === k} on:click={() => { status = k; load(); }}>{l}</button>
        {/each}
      </div>
      <label class="src">Source
        <select bind:value={source} on:change={load}>
          <option value="">Web + watch folder</option>
          <option value="ui">Web UI</option>
          <option value="scheduler">Watch folder</option>
        </select>
      </label>
    </div>

    {#if error}<div class="banner error">{error}</div>{/if}

    <div class="list" role="list">
      <div class="head" aria-hidden="true">
        <span>When</span><span>File</span><span>Statement</span><span>Account</span><span>Result</span><span class="num">Added</span><span class="num">Skipped</span><span class="num">Held</span>
      </div>
      {#each rows as r (r.id)}
        {@const st = STATUS[r.status] || STATUS.parsed}
        {@const d = open[r.id]}
        <div class="item" class:expanded={!!d} role="listitem">
          <button class="row" on:click={() => toggle(r)} aria-expanded={!!d}>
            <span class="c-when"><span>{when(r.created)}</span><small class="muted">{ago(r.created)}</small></span>
            <span class="c-file">
              <span class="src-ico" title={r.source === 'ui' ? 'Uploaded in the web UI' : 'Dropped in the watch folder'}>
                <Icon name={r.source === 'ui' ? 'upload' : 'folder'} size={15} />
              </span>
              <span class="ellip">{r.filename}</span>
            </span>
            <span class="c-stmt"><span class="ellip">{r.label || '—'}</span><small class="muted">{period(r)}</small></span>
            <span class="c-acct ellip">{r.account_name || '—'}</span>
            <span class="c-res"><span class="pill {st.tone}"><Icon name={st.icon} size={13} stroke={2.5} />{st.label}</span>
              {#if r.reconcile}
                <small class="rec" class:off={!r.reconcile.reconciled}>{r.reconcile.reconciled ? 'balance matches' : `gap ${money(r.reconcile.gap)}`}</small>
              {/if}
            </span>
            <span class="num mono">{r.status === 'failed' || r.status === 'parsed' ? '' : r.added}</span>
            <span class="num mono">{r.status === 'failed' || r.status === 'parsed' ? '' : r.skipped}</span>
            <span class="num mono" class:warnc={r.held}>{r.held || ''}</span>
          </button>

          {#if d}
            <div class="detail">
              {#if d.loading}<span class="spinner"></span>
              {:else if d.loadError}<div class="banner error">{d.loadError}</div>
              {:else}
                {#if d.status === 'failed'}
                  <div class="banner error">
                    <span class="b-icon"><Icon name="alert" size={18} /></span>
                    <div><strong>Failed while {STAGE[d.stage] || d.stage}.</strong> {d.error}
                      {#if d.source === 'scheduler'}<div class="muted">The file is in the watch folder's <span class="mono">error/</span>. Fix the cause, delete that copy, then drop it in again.</div>{/if}
                    </div>
                  </div>
                {:else if d.status === 'parsed'}
                  <div class="muted">Uploaded and read ({d.parsed} transactions) but never imported.</div>
                {/if}

                <dl>
                  <dt>Source</dt><dd>{d.source === 'ui' ? 'Web UI' : 'Watch folder'}</dd>
                  <dt>File</dt><dd><span class="mono">{d.filename}</span> · {kb(d.size)} · <span class="mono" title={d.sha256}>sha256 {d.sha256?.slice(0, 12)}</span></dd>
                  {#if d.label}<dt>Statement</dt><dd>{d.label}{period(d) ? ` · ${period(d)}` : ''}{d.statement?.balance != null ? ` · closing ${d.statement.balance_is_owed ? 'owed ' : ''}${money(Math.round(d.statement.balance * 100))}` : ''} · {d.parsed} transactions</dd>{/if}
                  {#if d.account_name}<dt>Account</dt><dd>{d.account_name}{d.account_why ? ` — ${d.account_why}` : ''}</dd>{/if}
                  {#if d.stats}<dt>Categorised by</dt><dd>
                    {[['actual', 'your Actual rules'], ['history', 'your history'], ['seed', 'merchant rules'], ['llm', 'AI'], ['transfer', 'transfers']]
                      .filter(([k]) => d.stats[k]).map(([k, l]) => `${d.stats[k]} ${l}`).join(' · ') || '—'}{d.stats.unmapped ? ` · ${d.stats.unmapped} uncategorised` : ''}</dd>{/if}
                  {#if d.reconcile}<dt>Reconcile</dt><dd>
                    {#if d.reconcile.reconciled}Actual matched the bank balance{d.reconcile.as_of ? ` on ${day(d.reconcile.as_of)}` : ''}.
                    {:else}Gap of {money(d.reconcile.gap)} (bank {money(d.reconcile.expected_balance)}, Actual {money(d.reconcile.actual_balance)}){d.reconcile.fixes_queued ? ` — ${d.reconcile.fixes_queued} fix(es) sent to Review` : ''}.{/if}</dd>{/if}
                  {#if d.review?.length}<dt>Review items</dt><dd>
                    {Object.entries(d.review.reduce((a, x) => ({ ...a, [x.status]: (a[x.status] || 0) + 1 }), {})).map(([k, n]) => `${n} ${k}`).join(' · ')}
                    {#if d.review.some((x) => x.status === 'pending')} — <a href="/#review">open Review</a>{/if}</dd>{/if}
                  {#if d.errors?.length}<dt>Row errors</dt><dd class="errs">{d.errors.slice(0, 5).join(' · ')}{d.errors.length > 5 ? ` (+${d.errors.length - 5})` : ''}</dd>{/if}
                  <dt>Updated</dt><dd>{when(d.updated_at)}</dd>
                </dl>

                <div class="actions">
                  {#if d.has_file}<a class="btn ghost sm" href={`${API}/imports/${d.id}/file`} download><Icon name="download" size={15} /> Original file</a>{/if}
                  {#if d.status === 'imported' && d.added_ids?.length}
                    {#if confirmUndo === d.id}
                      <span class="confirm">Delete the {d.added_ids.length} transaction{d.added_ids.length === 1 ? '' : 's'} this import added to {d.account_name}?</span>
                      <button class="danger-btn sm" disabled={busy[d.id]} on:click={() => undo(d)}>{busy[d.id] ? 'Undoing…' : 'Yes, undo'}</button>
                      <button class="ghost sm" on:click={() => (confirmUndo = '')}>Cancel</button>
                    {:else}
                      <button class="ghost sm" on:click={() => (confirmUndo = d.id)}><Icon name="undo" size={15} /> Undo import</button>
                    {/if}
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>
      {:else}
        {#if !loading}
          <div class="empty">
            <Icon name="file" size={28} />
            <div>{status || source ? 'Nothing matches these filters.' : 'No statements yet. Upload one on the Import page, or drop it in the watch folder.'}</div>
            <small class="muted">Imports from before this page existed aren't listed.</small>
          </div>
        {/if}
      {/each}
      {#if loading && !rows.length}<div class="empty"><span class="spinner"></span></div>{/if}
    </div>
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 16px;
    --good: #45c08f; --bad: #ff7a7a; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
  .tile .t-value.sm { font-size: 20px; }
  .tile.bad { border-color: #5a2323; background: #2a1414; }
  .tile.bad .t-value { color: var(--bad); }
  .ellip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

  .filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; justify-content: space-between; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .src { flex-direction: row; align-items: center; gap: 8px; }

  .list { background: var(--panel); border: 1px solid var(--border-soft); border-radius: 14px; overflow: hidden; }
  .head, .row { display: grid; grid-template-columns: 150px minmax(140px, 1.2fr) minmax(150px, 1.3fr) minmax(110px, 1fr) 150px 64px 64px 52px; gap: 12px; align-items: center; padding: 10px 16px; }
  .head { font-size: 12px; color: var(--text3); text-transform: uppercase; letter-spacing: .06em; border-bottom: 1px solid var(--border-soft); }
  .item { border-bottom: 1px solid var(--border-soft); }
  .item:last-child { border-bottom: 0; }
  .row { width: 100%; background: none; border: 0; border-radius: 0; color: var(--text); text-align: left; font-size: 13.5px; font-weight: 400; justify-content: stretch; }
  .row:hover, .expanded .row { background: var(--surface); }
  .row > span { min-width: 0; }
  .c-when, .c-stmt, .c-res { display: flex; flex-direction: column; line-height: 1.35; }
  .c-when small, .c-stmt small { font-size: 12px; }
  .c-file { display: flex; gap: 8px; align-items: center; }
  .src-ico { color: var(--text3); display: inline-flex; }
  .num { text-align: right; }
  .warnc { color: var(--warn); }
  .pill { display: inline-flex; align-items: center; gap: 5px; width: fit-content; border-radius: 999px; padding: 1px 9px 1px 7px; font-size: 12px; font-weight: 500; border: 1px solid var(--border); color: var(--text2); }
  .pill.good { color: var(--good); border-color: var(--ok-border); background: var(--ok-bg); }
  .pill.bad { color: var(--bad); border-color: #5a2323; background: #2a1414; }
  .pill.warn { color: var(--warn); border-color: var(--warn-border); background: var(--warn-bg); }
  .rec { font-size: 12px; color: var(--good); }
  .rec.off { color: var(--warn); }

  .detail { padding: 6px 16px 16px; display: flex; flex-direction: column; gap: 12px; background: var(--surface); }
  .detail .banner { padding: 12px 14px; }
  dl { display: grid; grid-template-columns: 130px 1fr; gap: 6px 16px; font-size: 13.5px; }
  dt { color: var(--text3); }
  dd { min-width: 0; overflow-wrap: anywhere; }
  .errs { color: var(--bad); }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .btn { display: inline-flex; align-items: center; gap: 8px; text-decoration: none; border-radius: 8px; color: #c9cdd6; border: 1px solid var(--border); height: 34px; padding: 0 14px; font-size: 13px; font-weight: 500; }
  .btn:hover { background: var(--surface2); color: var(--text); }
  .confirm { font-size: 13px; color: var(--warn); }
  .empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 16px; color: var(--text2); text-align: center; }
  a { color: var(--accent); }

  @media (max-width: 900px) {
    .head { display: none; }
    .row { grid-template-columns: 1fr auto; grid-template-areas: "file res" "stmt res" "acct when"; gap: 4px 12px; padding: 12px 14px; }
    .c-file { grid-area: file; font-weight: 500; }
    .c-stmt { grid-area: stmt; }
    .c-acct { grid-area: acct; color: var(--text2); }
    .c-res { grid-area: res; align-items: flex-end; }
    .c-when { grid-area: when; text-align: right; }
    .c-when small { display: none; }
    .row > .num { display: none; }
    dl { grid-template-columns: 1fr; gap: 2px; }
    dt { margin-top: 6px; }
  }
</style>
