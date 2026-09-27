<!--
  HealthStatus — one dot in the top bar summarising /api/health; click for details.
  Polls every 60s (the LLM result is cached server-side, so polling costs nothing).
-->
<script>
  import { onMount, onDestroy } from 'svelte';

  const API = '/api';
  const LABELS = {
    backend: 'Backend', bridge: 'Actual bridge', actual_server: 'Actual server',
    scheduler: 'Scheduler', llm: 'LLM',
  };
  let report = null;
  let error = '';
  let open = false;
  let loading = false;
  let timer;

  async function load(refreshLLM = false) {
    loading = true;
    try {
      const res = await fetch(`${API}/health${refreshLLM ? '?llm=refresh' : ''}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      report = await res.json();
      error = '';
    } catch (e) {
      // nginx is up (we're rendering) but the backend isn't answering
      error = `Backend unreachable (${e.message})`;
      report = null;
    } finally { loading = false; }
  }

  onMount(() => { load(); timer = setInterval(load, 60000); });
  onDestroy(() => clearInterval(timer));

  $: status = error ? 'error' : report?.status ?? 'unknown';
  $: problems = report
    ? Object.entries(report.components).filter(([, c]) => ['error', 'degraded', 'stale'].includes(c.status))
    : [];
  $: summary = error ? 'Backend down'
    : !report ? 'Checking…'
    : problems.length ? problems.map(([k]) => LABELS[k] ?? k).join(', ') + (problems.length > 1 ? ' need' : ' needs') + ' attention'
    : 'All systems OK';

  function fmt(c) {
    const bits = [];
    if (c.latency_ms != null) bits.push(`${c.latency_ms} ms`);
    if (c.version) bits.push(`v${c.version}`);
    if (c.api_version && !c.version) bits.push(`api ${c.api_version}`);
    if (c.provider && c.status !== 'disabled') bits.push(`${c.provider} · ${c.model}`);
    if (c.age_s != null) bits.push(`beat ${c.age_s}s ago`);
    if (c.budget_loaded === false) bits.push('no budget loaded');
    if (c.cached) bits.push('cached');
    return bits.join(' · ');
  }
</script>

<div class="health">
  <button class="ghost icon-btn pill" on:click={() => open = !open} title={summary}>
    <span class="dot {status}"></span>
    <span class="label">{summary}</span>
  </button>

  {#if open}
    <div class="panel">
      <div class="panel-head">
        <strong>Service health</strong>
        <button class="ghost icon-btn" on:click={() => load(false)} disabled={loading}>↻</button>
        <button class="ghost icon-btn" on:click={() => open = false}>✕</button>
      </div>
      {#if error}
        <div class="row"><span class="dot error"></span><div><strong>Backend</strong><small>{error}</small></div></div>
      {:else if report}
        {#each Object.entries(report.components) as [key, c]}
          <div class="row">
            <span class="dot {c.status}"></span>
            <div>
              <strong>{LABELS[key] ?? key}</strong> <span class="st">{c.status}</span>
              {#if fmt(c)}<small>{fmt(c)}</small>{/if}
              {#if c.detail}<small class="detail">{c.detail}</small>{/if}
              {#if key === 'scheduler' && c.last_file}<small>last: {c.last_file} → {c.last_result ?? '…'}</small>{/if}
            </div>
            {#if key === 'llm' && c.status !== 'disabled'}
              <button class="ghost icon-btn test" on:click={() => load(true)} disabled={loading}>
                {loading ? '…' : 'Test now'}
              </button>
            {/if}
          </div>
        {/each}
        <small class="ts">checked {new Date(report.checked_at).toLocaleTimeString('en-SG')}</small>
      {/if}
    </div>
  {/if}
</div>

<style>
  .health { position: relative; }
  .pill { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .label { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text2); }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; background: #6b7280; display: inline-block; }
  .dot.ok { background: var(--accent2); }
  .dot.degraded, .dot.stale { background: var(--warn); }
  .dot.error { background: var(--danger); }
  .dot.disabled, .dot.unknown { background: #6b7280; }
  .panel {
    position: absolute; right: 0; top: calc(100% + 6px); z-index: 90; width: 360px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 12px; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 8px 24px #0006;
  }
  .panel-head { display: flex; align-items: center; gap: 6px; }
  .panel-head strong { flex: 1; font-size: 14px; }
  .row { display: flex; gap: 10px; align-items: flex-start; font-size: 13px; }
  .row .dot { margin-top: 5px; }
  .row > div { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .st { font-size: 11px; color: var(--text2); }
  small { font-size: 11px; color: var(--text2); word-break: break-word; }
  small.detail { color: var(--warn); }
  .test { font-size: 11px; white-space: nowrap; }
  .ts { text-align: right; }
</style>
