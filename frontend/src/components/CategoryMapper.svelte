<!--
  CategoryMapper — the seed rules produced a category (e.g. "Dining & Hawker") that
  doesn't exist in your Actual budget. Pick the Actual category it should mean, or
  create it. Choices are saved server-side as aliases (DATA_DIR/category_aliases.json),
  so the scheduler and future imports use them too.
-->
<script>
  import { createEventDispatcher, onMount } from 'svelte';
  export let transactions = [];          // rows with t.unmapped === true
  export let actualCategoryGroups = [];

  const API = '/api';
  const dispatch = createEventDispatcher();

  let choice = {};        // canonical → Actual category name
  let createMissing = {}; // canonical → bool
  let groupOf = {};       // canonical → taxonomy group
  let saving = false;
  let saveError = '';

  $: ourCategories = [...new Set(transactions.map(t => t.category))].sort();
  function txnCount(cat) { return transactions.filter(t => t.category === cat).length; }

  onMount(async () => {
    try {
      const tax = await (await fetch(`${API}/taxonomy`)).json();
      for (const [g, cats] of Object.entries(tax.groups || {})) for (const c of cats) groupOf[c] = g;
    } catch {}
    try {
      const { aliases } = await (await fetch(`${API}/aliases`)).json();
      choice = { ...aliases };
    } catch {}
  });

  async function save() {
    saving = true; saveError = '';
    try {
      const aliases = {};
      for (const cat of ourCategories) {
        if (choice[cat]) { aliases[cat] = choice[cat]; continue; }
        if (!createMissing[cat]) continue;
        const res = await fetch(`${API}/actual/categories`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: cat,
            groupId: actualCategoryGroups.find(g => g.name === groupOf[cat])?.id,
            groupName: groupOf[cat] || 'Imported',
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || 'create failed');
        aliases[cat] = cat;
      }
      const res = await fetch(`${API}/aliases`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ aliases }),
      });
      if (!res.ok) throw new Error('could not save aliases');
      dispatch('save', aliases);
    } catch (e) { saveError = e.message; }
    finally { saving = false; }
  }
</script>

<!-- svelte-ignore a11y-no-static-element-interactions a11y-click-events-have-key-events -->
<div class="overlay" on:click|self={() => dispatch('close')}>
  <div class="modal">
    <div class="modal-header">
      <h3>Map categories</h3>
      <span class="badge muted">{ourCategories.length} unmapped</span>
      <button class="ghost icon-btn" on:click={() => dispatch('close')}>✕</button>
    </div>

    <p class="modal-hint">
      These categories come from the built-in Singapore merchant rules but don't exist in your
      Actual budget. Point each one at an existing category, or create it. Saved for all future imports.
    </p>

    {#if saveError}<div class="error-msg">{saveError}</div>{/if}

    <div class="map-table">
      <div class="map-header">
        <span>Seed category</span>
        <span>→ Actual category</span>
        <span>Create</span>
      </div>
      {#each ourCategories as cat}
        {@const count = txnCount(cat)}
        <div class="map-row">
          <div class="our-cat">
            <span class="our-cat-name">{cat}</span>
            <span class="our-cat-count">{groupOf[cat] ?? ''} · {count} txn{count !== 1 ? 's' : ''}</span>
          </div>
          <select bind:value={choice[cat]}>
            <option value="">— choose —</option>
            {#each actualCategoryGroups as group}
              <optgroup label={group.name}>
                {#each (group.categories || []).filter(c => !c.hidden) as c}
                  <option value={c.name}>{c.name}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
          {#if !choice[cat]}
            <label class="row-label"><input type="checkbox" bind:checked={createMissing[cat]} /><span>Create</span></label>
          {:else}
            <span class="matched">✓</span>
          {/if}
        </div>
      {/each}
    </div>

    <div class="modal-footer">
      <button class="ghost" on:click={() => dispatch('close')}>Cancel</button>
      <button class="primary" on:click={save} disabled={saving}>{saving ? 'Saving…' : 'Save mapping'}</button>
    </div>
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0; background: #00000088;
    display: flex; align-items: center; justify-content: center; z-index: 100;
  }
  .modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
    width: 680px; max-width: 95vw; max-height: 85vh;
    display: flex; flex-direction: column; gap: 14px; padding: 20px;
  }
  .modal-header { display: flex; align-items: center; gap: 10px; }
  .modal-header h3 { font-size: 17px; font-weight: 700; flex: 1; }
  .modal-hint { font-size: 13px; color: var(--text2); }
  .map-table { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; max-height: 420px; }
  .map-header {
    display: grid; grid-template-columns: 1.2fr 1.5fr 0.6fr; gap: 10px;
    font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2);
    position: sticky; top: 0; background: var(--surface); padding: 0 4px 4px;
  }
  .map-row { display: grid; grid-template-columns: 1.2fr 1.5fr 0.6fr; gap: 10px; align-items: center; padding: 4px 0; }
  .our-cat { display: flex; flex-direction: column; gap: 2px; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; }
  .our-cat-name { font-size: 13px; font-weight: 500; }
  .our-cat-count { font-size: 11px; color: var(--text2); }
  .map-row select { width: 100%; font-size: 13px; }
  .matched { color: var(--accent2); font-weight: 700; text-align: center; }
  .modal-footer { display: flex; gap: 10px; justify-content: flex-end; }
</style>
