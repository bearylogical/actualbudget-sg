<!--
  RulesAudit — audit the live Actual rules/payees/categories and apply a sync plan.
  Everything is opt-in per part, previewed with a dry run first.
-->
<script>
  import { createEventDispatcher, onMount } from 'svelte';
  export let sampleDescriptions = [];

  const API = '/api';
  const dispatch = createEventDispatcher();

  let loading = true;
  let error = '';
  let report = null;
  let markdown = '';
  let allSeed = false;
  let parts = { aliases: true, categories: false, merge_payees: false, rules: true, duplicates: true, broken: false };
  let preview = null;
  let applying = false;
  let applied = null;
  let confirm = false;

  const PART_INFO = {
    aliases: ['Save category aliases', 'Map taxonomy names onto your closest existing Actual categories (stored in the app, Actual unchanged)'],
    categories: ['Create missing categories', 'Adds taxonomy categories your data needs but Actual lacks'],
    merge_payees: ['Merge raw payees', 'Merges payees like "GRAB*A-12 SINGAPORE SG" into "Grab" (moves their transactions)'],
    rules: ['Create rules', 'Payee-rename (pre) and payee → category rules for merchants seen in your data'],
    duplicates: ['Delete duplicate rules', 'Removes exact duplicate copies, keeping one'],
    broken: ['Delete broken rules', 'Removes rules pointing at deleted categories/payees — review the list first'],
  };

  async function load() {
    loading = true; error = ''; preview = null;
    try {
      const res = await fetch(`${API}/rules/audit`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sample_descriptions: sampleDescriptions, all_seed: allSeed }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'audit failed');
      report = data.report; markdown = data.markdown;
    } catch (e) { error = e.message; }
    finally { loading = false; }
  }
  onMount(load);

  async function sync(dryRun) {
    applying = true; error = '';
    try {
      const res = await fetch(`${API}/rules/sync`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dryRun, parts, sample_descriptions: sampleDescriptions, all_seed: allSeed }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'sync failed');
      if (dryRun) preview = data;
      else { applied = data; confirm = false; preview = null; dispatch('applied', data); await load(); }
    } catch (e) { error = e.message; }
    finally { applying = false; }
  }

  function downloadMd() {
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown' }));
    const a = document.createElement('a'); a.href = url; a.download = 'actual-rules-audit.md'; a.click();
    URL.revokeObjectURL(url);
  }

  $: s = report?.summary;
</script>

<div class="view">
    <div class="view-head">
      <div>
        <h1>Rules</h1>
        <p class="muted sub">Audit your Actual rules and payees, then sync fixes back.</p>
      </div>
      <div class="view-actions">
        {#if markdown}<button class="ghost icon-btn" on:click={downloadMd}>⬇ Report</button>{/if}
        <button class="ghost icon-btn" on:click={load} disabled={loading} aria-label="Refresh">↻</button>
      </div>
    </div>

    {#if error}<div class="error-msg">{error}</div>{/if}

    {#if loading}
      <div class="center"><span class="spinner lg"></span></div>
    {:else if report}
      <div class="body">
        <div class="summary">
          <div class="chip"><strong>{s.rules}</strong><span>rules</span></div>
          <div class="chip"><strong>{s.payees}</strong><span>payees</span></div>
          <div class="chip" class:bad={s.broken}><strong>{s.broken}</strong><span>broken</span></div>
          <div class="chip" class:bad={s.duplicates}><strong>{s.duplicates}</strong><span>duplicates</span></div>
          <div class="chip" class:bad={s.conflicts}><strong>{s.conflicts}</strong><span>conflicts</span></div>
          <div class="chip" class:warn={s.raw_payees}><strong>{s.raw_payees}</strong><span>raw payees</span></div>
          <div class="chip" class:warn={s.disagreements}><strong>{s.disagreements}</strong><span>disagree w/ seed</span></div>
          <div class="chip"><strong>{s.proposed_rules}</strong><span>rules proposed</span></div>
        </div>

        {#if report.broken_rules.length}
          <details open><summary>Broken rules ({report.broken_rules.length})</summary>
            {#each report.broken_rules as b}<div class="row"><code>{b.rule}</code><span class="why">{b.reasons.join('; ')}</span></div>{/each}
          </details>
        {/if}
        {#if report.conflicting_rules.length}
          <details open><summary>Conflicting rules ({report.conflicting_rules.length}) — fix by hand in Actual</summary>
            {#each report.conflicting_rules as c}<div class="row">{#each c.rules as r}<code>{r}</code>{/each}</div>{/each}
          </details>
        {/if}
        {#if report.duplicate_rules.length}
          <details><summary>Duplicate rules ({report.duplicate_rules.length})</summary>
            {#each report.duplicate_rules as d}<div class="row"><code>{d.rule}</code><span class="why">×{d.remove.length + 1}</span></div>{/each}
          </details>
        {/if}
        {#if report.raw_payees.length}
          <details><summary>Raw payees to merge ({s.raw_payees} → {report.raw_payees.length})</summary>
            {#each report.raw_payees as g}
              <div class="row"><strong>{g.target}</strong>{#if !g.target_id}<span class="why">new</span>{/if}
                <span class="why">← {g.payees.slice(0, 5).map(p => p.name).join(' · ')}{g.payees.length > 5 ? ` … +${g.payees.length - 5}` : ''}</span></div>
            {/each}
          </details>
        {/if}
        {#if report.disagreements.length}
          <details><summary>Your rules vs seed table ({report.disagreements.length}) — Actual wins, just FYI</summary>
            {#each report.disagreements as d}<div class="row"><strong>{d.payee}</strong><span class="why">Actual: {d.actual_category} · seed: {d.seed_category}</span></div>{/each}
          </details>
        {/if}
        {#if report.missing_categories.length}
          <details><summary>Taxonomy categories not in Actual ({report.missing_categories.length})</summary>
            {#each report.missing_categories as m}<div class="row">{m.group} / <strong>{m.name}</strong>{#if m.suggested_alias}<span class="why">→ alias to “{m.suggested_alias}”</span>{/if}</div>{/each}
          </details>
        {/if}
        {#if report.plan.create_rules.length}
          <details><summary>Proposed rules ({report.plan.create_rules.length})</summary>
            {#each report.plan.create_rules as r}<div class="row"><span>{r.why}</span></div>{/each}
          </details>
        {/if}

        <div class="parts">
          <div class="parts-title">Sync</div>
          {#each Object.keys(PART_INFO) as k}
            <label class="part"><input type="checkbox" bind:checked={parts[k]} on:change={() => preview = null} />
              <span><strong>{PART_INFO[k][0]}</strong><small>{PART_INFO[k][1]}</small></span></label>
          {/each}
          <label class="part"><input type="checkbox" bind:checked={allSeed} on:change={load} />
            <span><strong>Propose rules for every seed merchant</strong><small>Default: only merchants that appear in your payees / this statement</small></span></label>
        </div>

        {#if preview}
          <div class="preview">
            Will apply: {preview.counts.aliases} aliases · {preview.counts.createCategories} categories ·
            {preview.counts.mergePayees} payee merges · {preview.counts.createRules} rules · {preview.counts.deleteRules} deletions
            <label class="confirm"><input type="checkbox" bind:checked={confirm} /> I've reviewed this — write to Actual</label>
          </div>
        {/if}
        {#if applied}
          <div class="success-msg">
            ✓ Applied: {applied.result?.categories ?? 0} categories, {applied.result?.merged ?? 0} payees merged,
            {applied.result?.rules ?? 0} rules, {applied.result?.deleted ?? 0} deleted
            {#if applied.result?.errors?.length}<br />⚠ {applied.result.errors.length} errors: {applied.result.errors.slice(0, 3).join(' | ')}{/if}
          </div>
        {/if}
      </div>

      <div class="modal-footer">
        <button class="ghost" on:click={() => sync(true)} disabled={applying}>🔍 Preview</button>
        <button class="primary" on:click={() => sync(false)} disabled={applying || !preview || !confirm}>
          {applying ? 'Applying…' : 'Apply to Actual'}
        </button>
      </div>
    {/if}
</div>

<style>
  .view { display: flex; flex-direction: column; gap: 14px; }
  .body { display: flex; flex-direction: column; gap: 10px; }
  .center { display: flex; justify-content: center; padding: 40px; }
  .summary { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }
  .chip { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; }
  .chip strong { font-size: 18px; }
  .chip span { font-size: 11px; color: var(--text2); }
  .chip.bad strong { color: var(--danger); }
  .chip.warn strong { color: var(--warn); }
  details { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; }
  summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  .row { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; font-size: 12px; padding: 5px 0; border-top: 1px solid var(--border); }
  .row:first-of-type { border-top: none; }
  .row code { font-size: 11px; word-break: break-all; }
  .why { color: var(--text2); font-size: 11px; }
  .parts { display: flex; flex-direction: column; gap: 6px; border-top: 1px solid var(--border); padding-top: 10px; }
  .parts-title { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2); }
  .part { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; cursor: pointer; }
  .part span { display: flex; flex-direction: column; }
  .part small { color: var(--text2); font-size: 11px; }
  .preview { background: var(--surface2); border: 1px dashed var(--warn); border-radius: 8px; padding: 10px; font-size: 13px; display: flex; flex-direction: column; gap: 6px; }
  .confirm { display: flex; gap: 8px; align-items: center; }
  .modal-footer { display: flex; gap: 10px; justify-content: flex-end; position: sticky; bottom: 0; padding: 12px 0; background: var(--bg); border-top: 1px solid var(--border-soft); }
</style>
