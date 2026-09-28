<!--
  Step 3 — preview (a dry run runs automatically), resolve possible duplicates,
  import, and offer undo. The button always names the destination account.
-->
<script>
  import { createEventDispatcher } from 'svelte';
  import Icon from '../Icon.svelte';
  import { readJson } from '../../lib/http.js';

  export let accountId = '';
  export let accountName = '';
  export let count = 0;                 // rows that will be sent
  export let uncategorised = 0;
  export let includeCredits = true;
  export let credits = 0;               // credit rows in the statement
  export let buildPayload;              // async (dryRun) => payload
  export let result = null;             // bound: last import result
  export let rulesCreated = 0;
  export let scanNote = '';

  const API = '/api';
  const dispatch = createEventDispatcher();
  let preview = null;
  let previewing = false;
  let importing = false;
  let undoing = false;
  let error = '';
  let verifications = {};

  $: toVerify = preview?.toVerify ?? [];
  $: unresolved = toVerify.filter((t) => !verifications[t.imported_id]);

  async function runPreview() {
    if (!accountId) return;
    previewing = true; error = ''; verifications = {};
    try {
      const payload = await buildPayload(true, {});
      preview = await readJson(await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      }));
    } catch (e) { error = e.message; }
    finally { previewing = false; }
  }
  // re-preview when what's being sent changes
  let lastKey = '';
  $: key = `${accountId}|${count}`;
  $: if (!result && key !== lastKey) { lastKey = key; runPreview(); }

  async function doImport() {
    importing = true; error = '';
    try {
      const payload = await buildPayload(false, verifications);
      result = await readJson(await fetch(`${API}/actual/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      }));
      dispatch('imported', result);
    } catch (e) { error = e.message; }
    finally { importing = false; }
  }

  async function undo() {
    if (!result?.addedIds?.length) return;
    undoing = true; error = '';
    try {
      await readJson(await fetch(`${API}/actual/undo-import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: result.addedIds }),
      }));
      result = null; lastKey = '';
      dispatch('undone');
    } catch (e) { error = e.message; }
    finally { undoing = false; }
  }

  function setAll(v) { verifications = Object.fromEntries(toVerify.map((t) => [t.imported_id, v])); }
</script>

{#if !result}
  <h1>Ready to import</h1>

  <div class="tiles">
    <div class="tile"><span class="t-label">New</span><span class="t-value">{previewing ? '…' : preview?.added ?? count}</span><span class="t-sub">will be added</span></div>
    <div class="tile"><span class="t-label">Already in Actual</span><span class="t-value">{previewing ? '…' : preview?.skipped ?? '—'}</span><span class="t-sub">skipped as duplicates</span></div>
    <div class="tile" class:warn={toVerify.length}><span class="t-label">Possible duplicates</span><span class="t-value">{previewing ? '…' : toVerify.length}</span><span class="t-sub">{toVerify.length ? 'decide below' : 'nothing to decide'}</span></div>
    <div class="tile"><span class="t-label">Uncategorised</span><span class="t-value">{uncategorised}</span><span class="t-sub">fixable later in Actual</span></div>
  </div>

  <div class="into">Going into <strong>{accountName}</strong>. {#if credits}{includeCredits ? `Includes ${credits} credit${credits === 1 ? '' : 's'} (deposits, refunds, interest).` : `${credits} credit${credits === 1 ? ' is' : 's are'} left out.`}{/if}</div>

  {#if toVerify.length}
    <div class="verify">
      <div class="v-head">
        <span><strong>{toVerify.length} possible duplicate{toVerify.length > 1 ? 's' : ''}.</strong> Skip or import each one.</span>
        <span class="v-bulk"><button class="ghost sm" on:click={() => setAll('skip')}>Skip all</button><button class="ghost sm" on:click={() => setAll('import')}>Import all</button></span>
      </div>
      {#each toVerify as t}
        <div class="v-row" class:done={!!verifications[t.imported_id]}>
          <span class="mono muted">{t.date}</span>
          <span class="v-desc">{t.description}{#if t.reason}<small class="muted"> — {t.reason}</small>{/if}</span>
          <span class="mono">{t.currency} {t.amount.toFixed(2)}</span>
          <span class="seg">
            <button class:on={verifications[t.imported_id] === 'skip'} on:click={() => (verifications = { ...verifications, [t.imported_id]: 'skip' })}>Skip</button>
            <button class:on={verifications[t.imported_id] === 'import'} on:click={() => (verifications = { ...verifications, [t.imported_id]: 'import' })}>Import</button>
          </span>
        </div>
      {/each}
    </div>
  {/if}

  {#if error}<div class="banner error">{error}</div>{/if}

  <div class="foot">
    <button class="ghost lg" on:click={() => dispatch('back')}>Back</button>
    <button class="primary lg" on:click={doImport} disabled={importing || previewing || !accountId || unresolved.length > 0 || (preview && preview.added === 0 && !toVerify.length)}>
      {#if importing}Importing…{:else if unresolved.length}Decide {unresolved.length} duplicate{unresolved.length > 1 ? 's' : ''} first{:else if preview && preview.added === 0 && !toVerify.length}Nothing new to import{:else}Import {preview?.added ?? count} into {accountName}{/if}
    </button>
  </div>
{:else}
  <h1>Imported</h1>
  <div class="banner ok">
    <span class="b-icon"><Icon name="check" size={22} stroke={2.5} /></span>
    <div class="ok-body">
      <div class="ok-title">{result.added} transaction{result.added === 1 ? '' : 's'} added to {accountName}</div>
      <div class="muted">
        {#if result.skipped}{result.skipped} skipped as duplicates. {/if}{#if result.updated}{result.updated} existing rows updated. {/if}{#if rulesCreated}{rulesCreated} category rule{rulesCreated === 1 ? '' : 's'} saved, so those payees are categorised automatically next time. {/if}Next, check that Actual's balance matches the bank.
      </div>
      {#if scanNote}<div class="muted">{scanNote} — <button class="linkish" on:click={() => dispatch('openReview')}>review</button></div>{/if}
    </div>
    {#if result.addedIds?.length}
      <button class="ghost sm" on:click={undo} disabled={undoing}><Icon name="undo" size={16} /> {undoing ? 'Undoing…' : 'Undo import'}</button>
    {/if}
  </div>
  {#if error}<div class="banner error">{error}</div>{/if}
  <div class="foot">
    <button class="ghost lg" on:click={() => dispatch('new')}>Import another statement</button>
    <button class="primary lg" on:click={() => dispatch('continue')}>Reconcile {accountName} <Icon name="arrow" /></button>
  </div>
{/if}

<style>
  h1 { font-size: 26px; font-weight: 600; letter-spacing: -.01em; }
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
  .into { font-size: 15px; color: #c9cdd6; }
  .into strong { color: var(--text); }
  .verify { border: 1px solid var(--warn-border); border-radius: 12px; overflow: auto; min-height: 0; }
  .v-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 10px 16px; background: var(--warn-bg); color: #f5deb3; font-size: 14px; }
  .v-bulk { display: flex; gap: 8px; }
  .v-row { display: grid; grid-template-columns: 96px minmax(0, 1fr) 120px auto; gap: 12px; align-items: center; padding: 8px 16px; border-top: 1px solid var(--border-soft); font-size: 14px; }
  .v-row.done { opacity: .6; }
  .v-desc { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .seg button { border-radius: 0; background: none; color: var(--text2); height: 30px; padding: 0 12px; font-size: 13px; }
  .seg button.on { background: var(--accent-fill); color: #fff; }
  .ok-body { flex: 1; display: flex; flex-direction: column; gap: 4px; }
  .ok-title { font-size: 17px; font-weight: 600; }
  .foot { margin-top: auto; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; }
</style>
