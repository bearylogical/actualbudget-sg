<!--
  Money — one view across Actual (cash, cards, spending) and Ghostfolio (investments),
  with deterministic spend guidance and an optional AI brief.
-->
<script>
  import { onMount } from 'svelte';
  import { readJson } from '../lib/http.js';

  const API = '/api';
  let data = null;
  let error = '';
  let loading = true;
  let brief = null;
  let briefLoading = false;
  let showTable = false;
  let hover = null;            // { i, x } for the cash-flow tooltip

  async function load() {
    loading = true; error = '';
    try { data = await readJson(await fetch(`${API}/finance/summary?months=7`)); }
    catch (e) { error = e.message; }
    finally { loading = false; }
  }
  onMount(load);

  async function getBrief() {
    briefLoading = true;
    try { brief = await readJson(await fetch(`${API}/finance/brief`, { method: 'POST' })); }
    catch (e) { brief = { error: e.message }; }
    finally { briefLoading = false; }
  }

  const fmt = (c, dp = 0) => c == null ? '—' : (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD', maximumFractionDigits: dp, minimumFractionDigits: dp });
  const pct = (v) => v == null ? '—' : `${Math.round(v * 100)}%`;
  const monthLabel = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString('en-SG', { month: 'short' });
  const ICON = { good: '✓', warning: '!', serious: '▲', critical: '✕', info: 'i' };
  const LABEL = { good: 'Good', warning: 'Watch', serious: 'Act', critical: 'Urgent', info: 'Note' };

  // ── cash-flow chart geometry ─────────────────────────────────────────────
  let cw = 640;                               // chart width follows its container
  const H = 240, PAD = { l: 48, r: 12, t: 12, b: 28 };
  $: W = Math.max(320, cw);
  $: flows = data?.cashflow ?? [];
  $: maxV = Math.max(1, ...flows.flatMap(f => [f.income, f.spent]));
  $: step = niceStep(maxV / 4);
  $: top = Math.ceil(maxV / step) * step;
  $: ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  $: groupW = (W - PAD.l - PAD.r) / Math.max(1, flows.length);
  $: barW = Math.min(22, groupW * 0.28);
  const y = (v) => PAD.t + (H - PAD.t - PAD.b) * (1 - v / (top || 1));
  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
    return [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw) ?? 10 * p;
  }
  // rounded data-end, square baseline (4px radius)
  function bar(x, v, w) {
    const y0 = y(0), y1 = y(v), h = Math.max(0, y0 - y1), r = Math.min(4, h, w / 2);
    if (h <= 0) return '';
    return `M${x},${y0} V${y1 + r} Q${x},${y1} ${x + r},${y1} H${x + w - r} Q${x + w},${y1} ${x + w},${y1 + r} V${y0} Z`;
  }
  const axisMoney = (c) => c >= 100000 ? `${+(c / 100000).toFixed(1)}k` : `${Math.round(c / 100)}`;
</script>

<div class="page">
  <div class="view-head">
    <div>
      <h1>Money</h1>
      <p class="muted sub">Cash, cards and spending from Actual, investments from Ghostfolio{data ? ` · as of ${data.as_of}` : ''}.</p>
    </div>
    <div class="view-actions">
      <button class="ghost icon-btn" on:click={load} disabled={loading} aria-label="Refresh">↻</button>
    </div>
  </div>

  {#if error}<div class="error-msg">{error}</div>{/if}
  {#if loading && !data}<div class="center"><span class="spinner lg"></span></div>{/if}

  {#if data}
    <div class="main">
      <!-- hero tiles -->
      <section class="tiles">
        <div class="tile hero"><span>Net worth</span><strong>{fmt(data.net_worth)}</strong>
          <small>cash & cards in Actual{data.investments_detail.configured ? ' + Ghostfolio' : ''}</small></div>
        <div class="tile"><span>Cash</span><strong>{fmt(data.cash)}</strong><small>on-budget accounts</small></div>
        <div class="tile"><span>Investments</span><strong>{data.investments_detail.configured ? fmt(data.investments) : '—'}</strong>
          <small>{data.investments_detail.configured
            ? (data.investments_detail.error ? '⚠ ' + data.investments_detail.error : `YTD ${data.investments_detail.performance_ytd != null ? (data.investments_detail.performance_ytd * 100).toFixed(1) + '%' : '—'}`)
            : 'connect Ghostfolio'}</small></div>
        <div class="tile"><span>Card owed</span><strong>{fmt(data.card_owed)}</strong><small>credit cards</small></div>
        <div class="tile"><span>Savings rate</span><strong>{pct(data.savings_rate)}</strong><small>last 3 full months</small></div>
        <div class="tile"><span>Emergency buffer</span><strong>{data.emergency_months != null ? data.emergency_months.toFixed(1) + ' mo' : '—'}</strong><small>cash ÷ avg monthly spend</small></div>
      </section>

      <div class="grid">
        <!-- this month -->
        <section class="card">
          <h2>This month</h2>
          {#if data.month.usual_by_today != null}
            {@const m = data.month}
            {@const scale = Math.max(m.projected ?? 0, data.avg_spend ?? 0, m.spent, 1)}
            <div class="meter" role="img" aria-label="Spent {fmt(m.spent)} of a usual {fmt(data.avg_spend)}">
              <div class="track"></div>
              <div class="fill" class:over={m.spent > m.usual_by_today * 1.15} style="width:{Math.min(100, m.spent / scale * 100)}%"></div>
              <div class="tick" style="left:{m.usual_by_today / scale * 100}%" title="usual by today"></div>
              <div class="tick avg" style="left:{(data.avg_spend ?? 0) / scale * 100}%" title="usual month"></div>
            </div>
            <div class="legend-row">
              <span><i class="sw fill-sw"></i>Spent {fmt(m.spent)}</span>
              <span><i class="sw tick-sw"></i>Usual by day {m.day}: {fmt(m.usual_by_today)}</span>
              <span><i class="sw avg-sw"></i>Usual month: {fmt(data.avg_spend)}</span>
            </div>
            <div class="kv">
              <div><span>Projected</span><strong>{fmt(m.projected)}</strong></div>
              <div><span>Safe to spend</span><strong>{m.safe_per_day != null ? fmt(m.safe_per_day) + '/day' : '—'}</strong></div>
              <div><span>Days left</span><strong>{m.days_left}</strong></div>
            </div>
            <small class="muted">Safe-to-spend is based on {m.safe_basis}.</small>
          {:else}
            <p class="muted">Needs at least one full month of history in Actual.</p>
          {/if}
        </section>

        <!-- guidance -->
        <section class="card">
          <div class="h2row"><h2>Guidance</h2>
            <button class="ghost small" on:click={getBrief} disabled={briefLoading}>{briefLoading ? '…' : '🤖 AI brief'}</button></div>
          {#if brief}
            {#if brief.error}<div class="error-msg">{brief.error}</div>
            {:else}<ul class="brief">{#each brief.bullets as b}<li>{b}</li>{/each}</ul>{/if}
          {/if}
          <ul class="guide">
            {#each data.guidance as gd}
              <li class="g {gd.level}">
                <span class="icon" aria-hidden="true">{ICON[gd.level]}</span>
                <div><strong>{gd.title}</strong> <span class="lvl">{LABEL[gd.level]}</span><br /><small>{gd.detail}</small></div>
              </li>
            {/each}
          </ul>
          {#if data.review_pending}<a class="small-link" href="/#review">Open Review ({data.review_pending}) →</a>{/if}
        </section>
      </div>

      <!-- cash flow -->
      <section class="card">
        <div class="h2row">
          <h2>Cash flow</h2>
          <div class="legend"><span><i class="sw s1"></i>Income</span><span><i class="sw s2"></i>Spending</span></div>
          <button class="ghost small" on:click={() => showTable = !showTable}>{showTable ? 'Chart' : 'Table'}</button>
        </div>
        {#if showTable}
          <table class="tbl"><thead><tr><th>Month</th><th class="r">Income</th><th class="r">Spending</th><th class="r">Net</th></tr></thead>
            <tbody>{#each flows as f}<tr><td>{f.month}</td><td class="r">{fmt(f.income)}</td><td class="r">{fmt(f.spent)}</td><td class="r">{fmt(f.income - f.spent)}</td></tr>{/each}</tbody></table>
        {:else}
          <div class="chart" bind:clientWidth={cw} on:mouseleave={() => hover = null} role="img" aria-label="Monthly income and spending">
            <svg viewBox="0 0 {W} {H}" width={W} height={H}>
              {#each ticks as t}
                <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} class="grid-line" />
                <text x={PAD.l - 8} y={y(t) + 4} class="axis" text-anchor="end">{axisMoney(t)}</text>
              {/each}
              {#each flows as f, i}
                {@const gx = PAD.l + i * groupW + groupW / 2}
                <path d={bar(gx - barW - 1, f.income, barW)} class="s1" />
                <path d={bar(gx + 1, f.spent, barW)} class="s2" />
                <rect x={PAD.l + i * groupW} y={PAD.t} width={groupW} height={H - PAD.t - PAD.b} fill="transparent"
                  on:mouseenter={() => hover = { i, x: (gx / W) * 100 }} />
                <text x={gx} y={H - 8} class="axis" text-anchor="middle">{monthLabel(f.month)}</text>
              {/each}
            </svg>
            {#if hover}
              {@const f = flows[hover.i]}
              <div class="tip" style="left:{hover.x}%">
                <strong>{f.month}</strong>
                <div><i class="sw s1"></i>Income {fmt(f.income)}</div>
                <div><i class="sw s2"></i>Spending {fmt(f.spent)}</div>
                <div class="muted">Net {fmt(f.income - f.spent)}</div>
              </div>
            {/if}
          </div>
        {/if}
      </section>

      <div class="grid">
        <!-- categories -->
        <section class="card">
          <h2>Categories vs your usual month</h2>
          {#each data.categories as c}
            {@const scale = Math.max(c.avg, c.spent, 1)}
            <div class="cat">
              <div class="cat-top">
                <span>{c.name}</span>
                {#if c.status !== 'good'}<span class="lvl {c.status}">{ICON[c.status]} {LABEL[c.status]}</span>{/if}
                <span class="num">{fmt(c.spent)} <span class="muted">/ {fmt(c.avg)}</span></span>
              </div>
              <div class="meter slim">
                <div class="track"></div>
                <div class="fill {c.status}" style="width:{c.spent / scale * 100}%"></div>
                {#if c.avg}<div class="tick" style="left:{c.expected_by_today / scale * 100}%" title="usual by today"></div>{/if}
              </div>
            </div>
          {:else}<p class="muted">No categorised spending yet this month.</p>{/each}
        </section>

        <!-- accounts & investments -->
        <section class="card">
          <h2>Accounts</h2>
          <table class="tbl">
            <tbody>
              {#each data.accounts as a}
                <tr><td>{a.name}{a.offbudget ? ' (off-budget)' : ''}</td><td class="r" class:neg={a.balance < 0}>{fmt(a.balance, 2)}</td></tr>
              {/each}
              {#if data.investments_detail.configured && !data.investments_detail.error}
                {#each data.investments_detail.accounts as a}
                  <tr><td>{a.name} <span class="muted">· Ghostfolio{a.platform ? ` · ${a.platform}` : ''}</span></td><td class="r">{a.value != null ? fmt(a.value * 100, 2) : '—'}</td></tr>
                {/each}
              {/if}
            </tbody>
          </table>
          {#if data.investments_detail.holdings?.length}
            <h3>Top holdings</h3>
            <table class="tbl">
              <thead><tr><th>Holding</th><th class="r">Value</th><th class="r">Weight</th><th class="r">Return</th></tr></thead>
              <tbody>
                {#each data.investments_detail.holdings as h}
                  <tr><td>{h.name} <span class="muted">{h.symbol}</span></td><td class="r">{h.value != null ? fmt(h.value * 100) : '—'}</td>
                    <td class="r">{h.allocation != null ? (h.allocation * 100).toFixed(1) + '%' : '—'}</td>
                    <td class="r">{h.performance != null ? (h.performance * 100).toFixed(1) + '%' : '—'}</td></tr>
                {/each}
              </tbody>
            </table>
          {:else if !data.investments_detail.configured}
            <p class="muted">Set <code>GHOSTFOLIO_URL</code> and <code>GHOSTFOLIO_TOKEN</code> to include investments.</p>
          {/if}
          <small class="muted">Guidance covers your own cash flow only — not investment advice.</small>
        </section>
      </div>
    </div>
  {/if}
</div>

<style>
  .page { display: flex; flex-direction: column; gap: 16px;
    --s1: #3987e5; --s2: #d95926;                          /* validated categorical slots (dark) */
    --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b; }
  .main { display: flex; flex-direction: column; gap: 16px; }
  .center { display: flex; justify-content: center; padding: 80px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 2px; }
  .tile span { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2); }
  .tile strong { font-size: 22px; font-variant-numeric: tabular-nums; }
  .tile.hero strong { font-size: 26px; }
  .tile small { color: var(--text2); font-size: 11px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  h2 { font-size: 14px; font-weight: 700; }
  h3 { font-size: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; margin-top: 6px; }
  .h2row { display: flex; align-items: center; gap: 12px; }
  .h2row h2 { flex: 1; }
  .muted { color: var(--text2); }
  small { font-size: 12px; }
  .small { font-size: 12px; padding: 4px 10px; }
  .small-link { font-size: 12px; color: var(--accent); text-decoration: none; }

  .meter { position: relative; height: 14px; }
  .meter.slim { height: 8px; }
  .meter .track { position: absolute; inset: 0; background: var(--surface3); border-radius: 4px; }
  .meter .fill { position: absolute; left: 0; top: 0; bottom: 0; background: var(--s1); border-radius: 4px; }
  .meter .fill.over, .meter .fill.serious { background: var(--serious); }
  .meter .fill.warning { background: var(--warning); }
  .meter .tick { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--text); border-radius: 1px; }
  .meter .tick.avg { background: var(--text2); }
  .legend-row { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--text2); }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
  .fill-sw, .s1.sw { background: var(--s1); }
  .s2.sw { background: var(--s2); }
  .tick-sw { background: var(--text); width: 3px; }
  .avg-sw { background: var(--text2); width: 3px; }
  .kv { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .kv div { background: var(--surface2); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; }
  .kv span { font-size: 11px; color: var(--text2); }
  .kv strong { font-variant-numeric: tabular-nums; }

  .guide { list-style: none; display: flex; flex-direction: column; gap: 8px; }
  .g { display: flex; gap: 10px; align-items: flex-start; font-size: 13px; }
  .g .icon { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; color: #111; flex-shrink: 0; background: var(--text2); }
  .g.good .icon { background: var(--good); }
  .g.warning .icon { background: var(--warning); }
  .g.serious .icon { background: var(--serious); }
  .g.critical .icon { background: var(--critical); color: #fff; }
  .g small { color: var(--text2); }
  .lvl { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2); border: 1px solid var(--border); border-radius: 999px; padding: 0 6px; }
  .lvl.warning { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 40%, transparent); }
  .lvl.serious { color: var(--serious); border-color: color-mix(in srgb, var(--serious) 40%, transparent); }
  .brief { font-size: 13px; padding-left: 18px; display: flex; flex-direction: column; gap: 4px; background: var(--surface2); border-radius: 8px; padding: 10px 10px 10px 26px; }

  .legend { display: flex; gap: 14px; font-size: 12px; color: var(--text2); }
  .chart { position: relative; }
  svg { display: block; }
  .grid-line { stroke: var(--border); stroke-width: 1; }
  .axis { fill: var(--text2); font-size: 11px; }
  path.s1 { fill: var(--s1); }
  path.s2 { fill: var(--s2); }
  .tip { position: absolute; top: 8px; transform: translateX(-50%); background: var(--surface3); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; pointer-events: none; white-space: nowrap; }

  .cat { display: flex; flex-direction: column; gap: 4px; }
  .cat-top { display: flex; gap: 8px; align-items: baseline; font-size: 13px; }
  .cat-top > span:first-child { flex: 1; }
  .num { font-variant-numeric: tabular-nums; }
  .tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
  .tbl td, .tbl th { padding: 6px 4px; border-bottom: 1px solid var(--border); text-align: left; }
  .tbl th { font-size: 11px; color: var(--text2); text-transform: uppercase; letter-spacing: .05em; font-weight: 500; }
  .r { text-align: right !important; font-variant-numeric: tabular-nums; }
  .neg { color: var(--serious); }
  @media (max-width: 700px) {
    .grid { grid-template-columns: 1fr; } .kv { grid-template-columns: 1fr 1fr; }
  }
</style>
