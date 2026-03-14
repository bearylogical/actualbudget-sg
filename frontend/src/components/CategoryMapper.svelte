<script>
  import { createEventDispatcher } from 'svelte';
  export let spending = [];
  export let categoryMap = {};
  export let actualCategoryGroups = [];

  const API = '/api';
  const dispatch = createEventDispatcher();

  let localMap = { ...categoryMap };
  let createMissing = {};
  let saving = false;
  let saveError = '';

  $: ourCategories = [...new Set(spending.map(t => t.category))].sort();
  $: unmapped = ourCategories.filter(c => !localMap[c] && !createMissing[c]);
  $: mapped = ourCategories.filter(c => localMap[c]);

  function txnCount(cat) { return spending.filter(t => t.category === cat).length; }

  async function save() {
    // Create any missing categories first
    const toCreate = ourCategories.filter(c => createMissing[c] && !localMap[c]);
    saving = true; saveError = '';
    try {
      for (const name of toCreate) {
        const res = await fetch(`${API}/actual/categories`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, groupName: 'Imported' })
        });
        const data = await res.json();
        if (res.ok) localMap = { ...localMap, [name]: data.id };
      }
      dispatch('save', localMap);
    } catch (e) { saveError = e.message; }
    finally { saving = false; }
  }
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="overlay" on:click|self={() => dispatch('close')}>
  <div class="modal">
    <div class="modal-header">
      <h3>Category Mapping</h3>
      <div class="modal-stats">
        <span class="badge">{mapped.length}/{ourCategories.length} mapped</span>
        {#if unmapped.length}<span class="badge muted">{unmapped.length} unmapped</span>{/if}
      </div>
      <button class="ghost icon-btn" on:click={() => dispatch('close')}>✕</button>
    </div>

    <p class="modal-hint">
      Match your parsed categories to Actual Budget categories. Unmapped transactions will import without a category.
    </p>

    {#if saveError}<div class="error-msg">{saveError}</div>{/if}

    <div class="map-table">
      <div class="map-header">
        <span>Parsed Category</span>
        <span>→ Actual Category</span>
        <span>Create in Actual</span>
      </div>
      {#each ourCategories as cat}
        {@const count = txnCount(cat)}
        <div class="map-row" class:unmapped={!localMap[cat]}>
          <div class="our-cat">
            <span class="our-cat-name">{cat}</span>
            <span class="our-cat-count">{count} txn{count !== 1 ? 's' : ''}</span>
          </div>
          <select bind:value={localMap[cat]}>
            <option value="">— skip —</option>
            {#each actualCategoryGroups as group}
              <optgroup label={group.name}>
                {#each group.categories || [] as c}
                  <option value={c.id}>{c.name}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
          {#if !localMap[cat]}
            <label class="row-label">
              <input type="checkbox" bind:checked={createMissing[cat]} />
              <span>Create</span>
            </label>
          {:else}
            <span class="matched">✓</span>
          {/if}
        </div>
      {/each}
    </div>

    {#if unmapped.filter(c => !createMissing[c]).length}
      <p class="warn-hint">⚠️ {unmapped.filter(c => !createMissing[c]).length} categories will import without a tag. Check "Create" to add them to Actual.</p>
    {/if}

    <div class="modal-footer">
      <button class="ghost" on:click={() => dispatch('close')}>Cancel</button>
      <button class="primary" on:click={save} disabled={saving}>
        {saving ? 'Saving…' : 'Save Mapping'}
      </button>
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
  .modal-stats { display: flex; gap: 6px; }
  .modal-hint { font-size: 13px; color: var(--text2); }

  .map-table { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; max-height: 400px; }
  .map-header {
    display: grid; grid-template-columns: 1.2fr 1.5fr 0.6fr; gap: 10px;
    font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text2);
    padding: 0 4px; position: sticky; top: 0; background: var(--surface); padding-bottom: 4px;
  }
  .map-row {
    display: grid; grid-template-columns: 1.2fr 1.5fr 0.6fr;
    gap: 10px; align-items: center; padding: 4px 0;
  }
  .map-row.unmapped { background: #f7931e06; border-radius: 6px; padding: 4px 4px; }
  .our-cat { display: flex; flex-direction: column; gap: 2px; background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; }
  .our-cat-name { font-size: 13px; font-weight: 500; }
  .our-cat-count { font-size: 11px; color: var(--text2); }
  .map-row select { width: 100%; font-size: 13px; }
  .matched { color: var(--accent2); font-weight: 700; text-align: center; }
  .warn-hint { font-size: 12px; color: var(--warn); }

  .modal-footer { display: flex; gap: 10px; justify-content: flex-end; }
</style>
