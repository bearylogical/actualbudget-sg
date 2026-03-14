<script>
  import '../app.css';
  import ActualPanel from '../components/ActualPanel.svelte';

  const API = '/api';

  const CATEGORIES = [
    'Food & Dining', 'Groceries', 'Transport', 'Shopping', 'Electronics',
    'Bills & Utilities', 'Software & Cloud', 'Subscriptions', 'Health & Fitness',
    'Health & Medical', 'Entertainment', 'Travel', 'Payment / Transfer',
    'Insurance', 'Work / Corporate', 'Donations', 'Education', 'Personal Care',
    'Uncategorized'
  ];

  const CAT_COLORS = {
    'Food & Dining': '#f7931e', 'Groceries': '#4caf50', 'Transport': '#2196f3',
    'Shopping': '#e91e63', 'Electronics': '#9c27b0', 'Bills & Utilities': '#ff5722',
    'Software & Cloud': '#00bcd4', 'Subscriptions': '#673ab7', 'Health & Fitness': '#8bc34a',
    'Health & Medical': '#f44336', 'Entertainment': '#ff9800', 'Travel': '#03a9f4',
    'Payment / Transfer': '#607d8b', 'Insurance': '#795548', 'Work / Corporate': '#546e7a',
    'Donations': '#009688', 'Education': '#3f51b5', 'Personal Care': '#e91e63',
    'Uncategorized': '#455a64'
  };

  let transactions = [];
  let loading = false;
  let error = '';
  let dragover = false;
  let searchQuery = '';
  let filterCategory = 'All';
  let sortBy = 'date';
  let sortDesc = true;
  let editingId = null;
  let editingCategory = '';
  let view = 'upload';
  let detectedBank = '';

  $: filtered = transactions
    .filter(t => {
      const matchSearch = !searchQuery ||
        t.description.toLowerCase().includes(searchQuery.toLowerCase());
      const matchCat = filterCategory === 'All' || t.category === filterCategory;
      return matchSearch && matchCat;
    })
    .sort((a, b) => {
      let va = a[sortBy], vb = b[sortBy];
      if (sortBy === 'date') { va = new Date(a.date); vb = new Date(b.date); }
      if (sortBy === 'amount') { va = a.amount; vb = b.amount; }
      return sortDesc ? (vb > va ? 1 : -1) : (va > vb ? 1 : -1);
    });

  $: spending = transactions.filter(t => !t.is_credit);
  $: totalSpend = spending.reduce((s, t) => s + t.amount, 0);

  $: summaryData = (() => {
    const byCategory = {};
    for (const t of spending) {
      if (!byCategory[t.category]) byCategory[t.category] = 0;
      byCategory[t.category] += t.amount;
    }
    return Object.entries(byCategory)
      .map(([cat, total]) => ({ cat, total }))
      .sort((a, b) => b.total - a.total);
  })();

  async function uploadFile(file) {
    if (!file) return;
    loading = true; error = '';
    const fd = new FormData();
    fd.append('file', file);
    try {
      const res = await fetch(`${API}/parse`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      transactions = data.transactions.map((t, i) => ({ ...t, id: i }));
      detectedBank = data.bank || '';
      view = 'transactions';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  function onDrop(e) {
    dragover = false;
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  }

  function onFileInput(e) {
    uploadFile(e.target.files[0]);
  }

  function startEdit(t) {
    editingId = t.id;
    editingCategory = t.category;
  }

  function saveEdit(t) {
    transactions = transactions.map(tx =>
      tx.id === t.id ? { ...tx, category: editingCategory } : tx
    );
    editingId = null;
  }

  async function exportCSV() {
    const res = await fetch(`${API}/export/csv`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transactions: filtered })
    });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'budget_export.csv'; a.click();
    URL.revokeObjectURL(url);
  }

  function fmtAmt(n) {
    return n.toLocaleString('en-SG', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtDate(d) {
    return new Date(d).toLocaleDateString('en-SG', { day: '2-digit', month: 'short', year: 'numeric' });
  }

  function confidenceBadge(c) {
    if (c >= 0.9) return 'high';
    if (c >= 0.75) return 'med';
    return 'low';
  }
</script>

<div class="shell">
  <header>
    <div class="logo">
      <span class="logo-icon">💳</span>
      <span>Budget Parser</span>
    </div>
    {#if transactions.length}
      <nav>
        <button class:active={view === 'transactions'} class="nav-btn" on:click={() => view = 'transactions'}>
          Transactions <span class="badge">{transactions.length}</span>
        </button>
        <button class:active={view === 'summary'} class="nav-btn" on:click={() => view = 'summary'}>
          Summary
        </button>
        <button class:active={view === 'actual'} class="nav-btn actual-btn" on:click={() => view = 'actual'}>
          ⬆ Import to Actual
        </button>
        <button class="nav-btn" on:click={() => { transactions = []; detectedBank = ''; view = 'upload'; }}>
          ↩ New File
        </button>
      </nav>
    {/if}
  </header>

  <main>
    <!-- UPLOAD VIEW -->
    {#if view === 'upload'}
      <div class="upload-view">
        <h1>Bank Statement → Budget</h1>
        <p class="subtitle">Upload your bank statement. UOB, DBS/POSB, and OCBC are auto-detected. Transactions are categorized using keyword matching.</p>

        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div
          class="dropzone"
          class:dragover
          on:dragover|preventDefault={() => dragover = true}
          on:dragleave={() => dragover = false}
          on:drop|preventDefault={onDrop}
          on:click={() => document.getElementById('fileInput').click()}
        >
          {#if loading}
            <div class="spinner"></div>
            <p>Parsing statement…</p>
          {:else}
            <div class="drop-icon">📂</div>
            <p><strong>Drop your .xls / .xlsx file here</strong></p>
            <p class="hint">or click to browse</p>
          {/if}
        </div>
        <input id="fileInput" type="file" accept=".xls,.xlsx" style="display:none" on:change={onFileInput} />

        {#if error}
          <div class="error-box">⚠️ {error}</div>
        {/if}

        <div class="supported">
          <span>✓ UOB</span>
          <span>✓ DBS / POSB</span>
          <span>✓ OCBC</span>
          <span>✓ XLS / XLSX</span>
          <span>✓ Auto-categorization</span>
          <span>✓ CSV Export</span>
        </div>
      </div>

    <!-- TRANSACTIONS VIEW -->
    {:else if view === 'transactions'}
      <div class="txn-view">
        <div class="toolbar">
          <input placeholder="🔍 Search transactions…" bind:value={searchQuery} />
          <select bind:value={filterCategory}>
            <option value="All">All Categories</option>
            {#each CATEGORIES as c}
              <option value={c}>{c}</option>
            {/each}
          </select>
          <select bind:value={sortBy}>
            <option value="date">Sort: Date</option>
            <option value="amount">Sort: Amount</option>
            <option value="category">Sort: Category</option>
          </select>
          <button class="ghost" on:click={() => sortDesc = !sortDesc}>
            {sortDesc ? '↓' : '↑'}
          </button>
          <button class="success" on:click={exportCSV}>⬇ Export CSV</button>
        </div>

        <div class="stats-bar">
          <div class="stat">
            <span class="stat-label">Transactions</span>
            <span class="stat-value">{filtered.length}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Total Spend</span>
            <span class="stat-value">SGD {fmtAmt(filtered.filter(t => !t.is_credit).reduce((s,t) => s+t.amount, 0))}</span>
          </div>
          {#if detectedBank}
          <div class="stat">
            <span class="stat-label">Bank</span>
            <span class="stat-value">{detectedBank}</span>
          </div>
        {/if}
        <div class="stat">
            <span class="stat-label">Auto-Categorized</span>
            <span class="stat-value">{transactions.filter(t => t.category !== 'Uncategorized').length} / {transactions.length}</span>
          </div>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Category</th>
                <th>Confidence</th>
                <th style="text-align:right">Amount</th>
              </tr>
            </thead>
            <tbody>
              {#each filtered as t (t.id)}
                <tr class:credit={t.is_credit}>
                  <td class="date">{fmtDate(t.date)}</td>
                  <td class="desc">
                    <span>{t.description}</span>
                    {#if t.foreign_amount}
                      <span class="foreign">{t.foreign_currency} {t.foreign_amount}</span>
                    {/if}
                  </td>
                  <td class="cat-cell">
                    {#if editingId === t.id}
                      <select bind:value={editingCategory} on:change={() => saveEdit(t)} on:blur={() => saveEdit(t)}>
                        {#each CATEGORIES as c}
                          <option value={c}>{c}</option>
                        {/each}
                      </select>
                    {:else}
                      <button class="cat-badge" style="background:{CAT_COLORS[t.category]}22; color:{CAT_COLORS[t.category]}; border-color:{CAT_COLORS[t.category]}44"
                        on:click={() => startEdit(t)}>
                        {t.category} ✎
                      </button>
                    {/if}
                  </td>
                  <td>
                    {#if !t.is_credit}
                      <span class="conf conf-{confidenceBadge(t.confidence)}">
                        {Math.round(t.confidence * 100)}%
                      </span>
                    {/if}
                  </td>
                  <td class="amount" class:negative={t.is_credit}>
                    {t.is_credit ? '−' : ''}{t.currency} {fmtAmt(t.amount)}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>

    <!-- SUMMARY VIEW -->
    {:else if view === 'summary'}
      <div class="summary-view">
        <h2>Spending Summary</h2>
        <p class="subtitle">Total: <strong>SGD {fmtAmt(totalSpend)}</strong> across {spending.length} transactions</p>

        <div class="summary-grid">
          {#each summaryData as { cat, total }}
            <div class="summary-card">
              <div class="summary-header">
                <span class="cat-dot" style="background:{CAT_COLORS[cat]}"></span>
                <span class="cat-name">{cat}</span>
                <span class="cat-pct">{((total / totalSpend) * 100).toFixed(1)}%</span>
              </div>
              <div class="bar-wrap">
                <div class="bar" style="width:{(total/totalSpend)*100}%; background:{CAT_COLORS[cat]}"></div>
              </div>
              <div class="cat-total">SGD {fmtAmt(total)}</div>
            </div>
          {/each}
        </div>
      </div>

    <!-- ACTUAL BUDGET VIEW -->
    {:else if view === 'actual'}
      <div class="actual-view">
        <h2>Import to Actual Budget</h2>
        <p class="subtitle">Connect to your Actual server and push <strong>{transactions.filter(t => !t.is_credit).length}</strong> transactions directly into your budget.</p>
        <ActualPanel {transactions} />
      </div>
    {/if}
  </main>
</div>

<style>
  .shell { min-height: 100vh; display: flex; flex-direction: column; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 28px; background: var(--surface);
    border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10;
  }
  .logo { display: flex; align-items: center; gap: 10px; font-size: 18px; font-weight: 700; }
  .logo-icon { font-size: 22px; }

  nav { display: flex; gap: 8px; }
  .nav-btn {
    background: transparent; color: var(--text2); padding: 6px 14px;
    border: 1px solid transparent; border-radius: 8px; font-size: 13px;
  }
  .nav-btn:hover, .nav-btn.active { background: var(--surface2); border-color: var(--border); color: var(--text); }
  .badge {
    background: var(--accent); color: #fff; border-radius: 999px;
    padding: 1px 7px; font-size: 11px; margin-left: 4px;
  }

  main { flex: 1; padding: 32px 28px; max-width: 1200px; margin: 0 auto; width: 100%; }

  /* Upload */
  .upload-view { max-width: 560px; margin: 60px auto; text-align: center; }
  h1 { font-size: 32px; font-weight: 800; margin-bottom: 12px; }
  .subtitle { color: var(--text2); margin-bottom: 32px; }

  .dropzone {
    border: 2px dashed var(--border); border-radius: 16px; padding: 60px 40px;
    cursor: pointer; transition: all 0.2s; background: var(--surface);
    display: flex; flex-direction: column; align-items: center; gap: 12px;
  }
  .dropzone.dragover { border-color: var(--accent); background: var(--surface2); }
  .drop-icon { font-size: 48px; }
  .hint { color: var(--text2); font-size: 13px; }

  .spinner {
    width: 40px; height: 40px; border: 3px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .error-box { background: #ff6b6b22; border: 1px solid #ff6b6b44; color: #ff6b6b; border-radius: 8px; padding: 12px 16px; margin-top: 16px; }
  .supported { display: flex; gap: 16px; margin-top: 24px; justify-content: center; flex-wrap: wrap; }
  .supported span { background: var(--surface2); border: 1px solid var(--border); border-radius: 999px; padding: 4px 14px; font-size: 12px; color: var(--text2); }

  /* Transactions */
  .toolbar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
  .toolbar input { flex: 1; min-width: 200px; }

  .stats-bar { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 20px; }
  .stat-label { display: block; color: var(--text2); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  .stat-value { font-size: 18px; font-weight: 700; }

  .table-wrap { overflow-x: auto; border-radius: var(--radius); border: 1px solid var(--border); }
  table { width: 100%; border-collapse: collapse; }
  thead { background: var(--surface2); }
  th { padding: 11px 14px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--text2); font-weight: 600; }
  td { padding: 11px 14px; border-top: 1px solid var(--border); vertical-align: middle; }
  tr:hover td { background: var(--surface2); }
  tr.credit td { opacity: 0.5; }

  .date { white-space: nowrap; color: var(--text2); font-size: 13px; }
  .desc { max-width: 280px; }
  .desc span { display: block; }
  .foreign { color: var(--text2); font-size: 12px; }

  .cat-cell { white-space: nowrap; }
  .cat-badge {
    padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 500;
    border: 1px solid; background: transparent; white-space: nowrap;
  }
  .cat-badge:hover { opacity: 0.8; }

  .conf { font-size: 12px; padding: 2px 8px; border-radius: 999px; }
  .conf-high { background: #00c9a722; color: var(--accent2); }
  .conf-med  { background: #f7931e22; color: #f7931e; }
  .conf-low  { background: #ff6b6b22; color: var(--danger); }

  .amount { text-align: right; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .amount.negative { color: var(--accent2); }

  /* Summary */
  .summary-view h2 { font-size: 24px; margin-bottom: 8px; }
  .summary-grid { display: grid; gap: 14px; margin-top: 24px; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
  .summary-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; }
  .summary-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .cat-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .cat-name { flex: 1; font-weight: 500; }
  .cat-pct { color: var(--text2); font-size: 13px; }
  .bar-wrap { height: 6px; background: var(--surface2); border-radius: 999px; overflow: hidden; margin-bottom: 8px; }
  .bar { height: 100%; border-radius: 999px; transition: width 0.5s; }
  .cat-total { font-size: 20px; font-weight: 700; }

  /* Actual Budget nav button */
  .actual-btn { background: #6c63ff22 !important; border-color: var(--accent) !important; color: var(--accent) !important; }
  .actual-btn.active { background: var(--accent) !important; color: #fff !important; }

  /* Actual view */
  .actual-view { max-width: 680px; }
  .actual-view h2 { font-size: 24px; margin-bottom: 6px; }

</style>
