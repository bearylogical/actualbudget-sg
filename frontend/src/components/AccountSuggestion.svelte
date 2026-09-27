<!--
  AccountSuggestion — shows what the statement is (bank, card vs savings, last 4)
  and which Actual account it should go into, with the reasons. Emits `pick`.
-->
<script>
  import { createEventDispatcher } from 'svelte';
  export let statement = null;     // StatementInfo dict from the backend
  export let rec = null;           // { suggestions, recommended, auto_select, remembered }
  export let selectedId = '';
  export let total = 0;            // transactions in this statement

  const dispatch = createEventDispatcher();

  $: top = rec?.suggestions?.[0];
  $: recommended = rec?.recommended;
  $: selected = rec?.suggestions?.find(s => s.account_id === selectedId);
  $: onRecommended = recommended && selectedId === recommended;
  // confident recommendation but the user picked something else → warn
  $: mismatch = !!(rec?.auto_select && selectedId && selectedId !== recommended);
  // the same transactions already live in another account → importing here duplicates them
  $: holder = (rec?.suggestions || []).find(s =>
    s.account_id !== selectedId && (s.matched || 0) >= Math.max(3, 0.2 * total));
  $: conflict = !!(selectedId && holder && !(selected?.matched));
  $: alternatives = (rec?.suggestions || []).filter(s => s.account_id !== selectedId && s.score > 0).slice(0, 3);
  function money(v) { return v == null ? '' : v.toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }); }
</script>

{#if statement}
  <div class="acct-card" class:ok={onRecommended && !conflict} class:warn={mismatch || conflict}>
    {#if conflict}
      <div class="line"><strong>⛔ {holder.matched} of these {total} transactions are already in {holder.name}.
        Importing into this account would duplicate them.</strong>
        <button class="primary small" on:click={() => dispatch('pick', holder.account_id)}>Switch to {holder.name}</button>
      </div>
    {/if}
    <div class="line">
      <span class="k">Statement</span>
      <strong>{statement.label}</strong>
      {#if statement.period_start}<span class="muted">· {statement.period_start} → {statement.period_end}</span>{/if}
      {#if statement.balance != null}
        <span class="muted">· {statement.balance_is_owed ? 'owed' : 'balance'} {money(statement.balance)}</span>
      {/if}
    </div>

    {#if !rec?.suggestions?.length}
      <div class="line muted">Connect to Actual to get an account suggestion.</div>
    {:else if onRecommended}
      <div class="line">
        <span class="k">Account</span> ✓ <strong>{top.name}</strong>
        <span class="muted">— {top.reasons.join(' · ')}</span>
      </div>
    {:else}
      {#if mismatch}
        <div class="line"><strong>⚠ This statement looks like it belongs in {top.name}, not {selected?.name ?? 'the selected account'}.</strong></div>
      {/if}
      {#if recommended}
        <div class="line">
          <span class="k">Suggested</span>
          <button class="primary small" on:click={() => dispatch('pick', recommended)}>Use {top.name}</button>
          <span class="muted">{top.reasons.join(' · ') || 'best name match'}</span>
        </div>
      {:else}
        <div class="line muted">No confident match — pick the account in the sidebar; it'll be remembered after import.</div>
      {/if}
    {/if}

    {#if alternatives.length && !onRecommended}
      <div class="line alts">
        <span class="k">Others</span>
        {#each alternatives.filter(a => a.account_id !== recommended) as a}
          <button class="ghost small" title={a.reasons.join(' · ')} on:click={() => dispatch('pick', a.account_id)}>{a.name}</button>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .acct-card {
    margin: 10px 20px 0; padding: 10px 14px; border-radius: 10px;
    background: var(--surface); border: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px; font-size: 13px;
  }
  .acct-card.ok { border-color: color-mix(in srgb, var(--accent2) 45%, transparent); }
  .acct-card.warn { border-color: var(--warn); background: color-mix(in srgb, var(--warn) 8%, var(--surface)); }
  .line { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .k { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--text2); min-width: 72px; }
  .muted { color: var(--text2); }
  .small { font-size: 12px; padding: 3px 10px; }
</style>
