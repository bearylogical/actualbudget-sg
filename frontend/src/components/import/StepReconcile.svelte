<!--
  Step 4 — does Actual match the bank? Runs the rules engine straight away
  against the statement's closing balance, shows plain checks, and only offers
  the AI when the checks don't explain the gap. Fixes go to the review queue.
-->
<script>
  import { createEventDispatcher, onMount } from 'svelte';
  import Icon from '../Icon.svelte';
  import { readJson } from '../../lib/http.js';

  export let accountId = '';
  export let accountName = '';
  export let statement = null;
  export let count = 0;                 // statement rows that were imported
  export let aiAvailable = false;
  export let sessionId = '';
  export let initial = null;            // the reconcile the commit already ran (closing balance, no AI)

  const API = '/api';
  const dispatch = createEventDispatcher();
  let balance = '';
  let statementDate = '';               // card: the cycle cut-off printed on the e-statement
  let editBalance = false;
  let running = false;
  let usedAI = false;
  let res = null;
  let error = '';

  $: isCard = statement?.kind === 'credit_card';
  const money = (c) => (c == null ? '—' : (c / 100).toLocaleString('en-SG', { style: 'currency', currency: 'SGD' }));
  const day = (d) => (d ? new Date(d).toLocaleDateString('en-SG', { day: 'numeric', month: 'short' }) : '');

  async function run(useLLM = false) {
    running = true; error = ''; usedAI = useLLM;
    try {
      const body = { use_llm: useLLM };
      if (balance !== '' && balance != null) body.balance = isCard ? -Math.abs(Number(balance)) : Number(balance);
      if (isCard && statementDate) body.statement_date = statementDate;
      res = await readJson(await fetch(`${API}/imports/${sessionId}/reconcile`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      }));
      if (res.queued?.length) dispatch('queued', res.queued.length);
    } catch (e) { error = e.message; }
    finally { running = false; }
  }
  onMount(() => {
    if (initial?.report) { res = initial; if (res.queued?.length) dispatch('queued', res.queued.length); }
    else if (accountId && sessionId) run(false);
  });

  $: r = res?.report;
  $: cyc = r?.card_cycle;
  $: total = count;
  $: cycleCheck = !cyc ? [] : cyc.statement_date
    ? [{ level: cyc.cycle_gap === 0 ? 'ok' : 'warn',
         title: cyc.cycle_gap === 0
           ? `Statement balance ${money(-cyc.statement_balance)} matches Actual on ${day(cyc.statement_date)}`
           : `On the ${day(cyc.statement_date)} statement Actual is off by ${money(Math.abs(cyc.cycle_gap))}`,
         why: `Statement date ${cyc.source}. Rolled forward with ${cyc.posted_since} row${cyc.posted_since === 1 ? '' : 's'} posted since (${money(cyc.unbilled)}), you owe ${money(-cyc.expected_today)} today.` +
              (cyc.export_covers_cycle ? '' : ' The export starts after the statement date, so rows in between may be missing.') }]
    : [{ level: 'warn', title: 'Couldn’t find the statement date',
         why: `No day since ${day(cyc.searched_from)} has Actual at the statement balance of ${money(-cyc.statement_balance)}` +
              (cyc.nearest ? ` (closest ${day(cyc.nearest.date)}, off by ${money(Math.abs(cyc.nearest.gap))}).` : '.') +
              ' Enter the statement date from your e-statement for an exact check.' }];
  $: checks = !r ? [] : [
    ...cycleCheck,
    r.matched === 0 && r.missing.length
      ? { level: 'bad', title: `None of the ${r.missing.length} statement transactions are in ${accountName}`, why: 'This statement probably belongs to a different account.' }
      : r.missing.length
        ? { level: 'warn', title: `${r.missing.length} statement transaction${r.missing.length > 1 ? 's are' : ' is'} missing from ${accountName}`, why: `${r.matched} matched` }
        : { level: 'ok', title: 'Right account', why: `${r.matched} of ${r.matched} statement transactions found in ${accountName}` },
    r.duplicates.length
      ? { level: 'warn', title: `${r.duplicates.length} possible duplicate${r.duplicates.length > 1 ? 's' : ''}`, why: 'Same amount and date appear more than once' }
      : { level: 'ok', title: 'No duplicates', why: 'Nothing was entered twice' },
    ...(r.transfers.length
      ? [{ level: 'warn', title: `${r.transfers.length} transfer${r.transfers.length > 1 ? 's aren’t' : ' isn’t'} linked`,
          why: r.transfers.slice(0, 3).map((p) => `${money(Math.abs(p.a.amount))} between ${p.a.account_name} and ${p.b.account_name} on ${day(p.a.date)}`).join(' · ') }]
      : []),
    ...(r.extra.length
      ? [{ level: 'warn', title: `${r.extra.length} row${r.extra.length > 1 ? 's' : ''} in Actual aren’t on the statement`, why: 'Hand-entered, from another account, or a transfer counterpart' }]
      : []),
    ...(r.running_balance?.constant_offset != null && !r.reconciled
      ? [{ level: 'warn', title: `Opening balance missing (${money(r.running_balance.constant_offset)})`, why: 'Every day is off by the same amount, so the balance before the statement is missing' }]
      : []),
  ];
  $: direction = !r || r.reconciled ? '' : r.gap < 0 ? `Actual is ${money(-r.gap)} higher than the bank.` : `Actual is ${money(r.gap)} lower than the bank.`;
</script>

<div class="head">
  <h1>Reconcile {accountName}</h1>
  <div class="muted">
    {#if !editBalance}
      {#if balance !== ''}Bank balance you entered.
      {:else if cyc?.statement_date}Card statement balance on {day(cyc.statement_date)}, rolled forward with what's posted since.
      {:else}Bank balance taken from the statement's closing balance{r?.as_of ? ` on ${day(r.as_of)}` : ''}.{/if}
      <button class="linkish" on:click={() => (editBalance = true)}>{isCard ? 'Enter statement date or balance' : 'Use a different balance'}</button>
    {:else}
      {#if isCard}
        <label class="bal" title="The cut-off date on your monthly card e-statement — remembered for this card">Statement date
          <input type="date" bind:value={statementDate} max={r?.as_of} />
        </label>
      {/if}
      <label class="bal">{isCard ? 'Amount owed today' : 'Bank balance today'}
        <input type="number" step="0.01" bind:value={balance} placeholder={isCard ? 'optional' : statement?.balance ?? ''} />
        <button class="ghost sm" on:click={() => run(usedAI)} disabled={running}>Check again</button>
      </label>
    {/if}
  </div>
</div>

{#if error}<div class="banner error">{error}</div>{/if}

{#if running && !r}
  <div class="muted"><span class="spinner" style="width:14px;height:14px;vertical-align:-2px"></span> Comparing Actual with the statement…</div>
{/if}

{#if r}
  <div class="tiles">
    <div class="tile"><span class="t-label">Bank{r.as_of ? ` · ${day(r.as_of)}` : ''}</span><span class="t-value">{money(r.expected_balance)}</span></div>
    <div class="tile"><span class="t-label">Actual</span><span class="t-value">{money(r.actual_balance)}</span></div>
    <div class="tile" class:ok={r.reconciled} class:warn={!r.reconciled}><span class="t-label">Difference</span><span class="t-value">{r.reconciled ? '$0.00' : money(Math.abs(r.gap))}</span></div>
  </div>
  {#if !r.balance_reliable && !cyc}<div class="banner warn"><span class="b-icon"><Icon name="info" size={20} /></span><div>{r.basis}</div></div>{/if}

  {#if r.reconciled}
    <div class="banner ok">
      <span class="b-icon"><Icon name="check" size={22} stroke={2.5} /></span>
      <div class="grow"><strong>Balanced.</strong> <span class="muted">{r.matched} statement transactions matched in {accountName}.{#if cyc?.statement_date}
        Statement date {day(cyc.statement_date)} ({cyc.source}); {cyc.posted_since} row{cyc.posted_since === 1 ? '' : 's'} posted since.{/if}</span></div>
    </div>
  {:else}
    <div class="checks">
      <div class="lead"><strong>{direction}</strong> <span class="muted">Here's what was checked:</span></div>
      {#each checks as c}
        <div class="check {c.level}">
          <span class="c-icon"><Icon name={c.level === 'ok' ? 'check' : 'alert'} size={20} stroke={c.level === 'ok' ? 2.5 : 2} /></span>
          <div class="grow"><div class="c-title">{c.title}</div><div class="muted">{c.why}</div></div>
        </div>
      {/each}
    </div>

    {#if res.queued?.length}
      <div class="fixes">
        <div class="grow"><strong>{res.queued.length} fix{res.queued.length > 1 ? 'es' : ''} ready for your approval</strong>
          <div class="muted">{r.recommendation.map((p) => p.title).slice(0, 3).join(' · ')}</div></div>
        <button class="primary sm" on:click={() => dispatch('openReview')}>Review fixes</button>
      </div>
    {/if}

    {#if res.explanation}
      <div class="ai"><span class="c-icon"><Icon name="sparkle" size={18} /></span><div class="grow">{res.explanation}</div></div>
    {:else if aiAvailable}
      <div class="ask muted">Still doesn't add up?
        <button class="linkish" on:click={() => run(true)} disabled={running}>{running && usedAI ? 'Asking…' : 'Ask AI to look for the difference'}</button>
      </div>
    {/if}
  {/if}
{/if}

<div class="foot">
  <button class="ghost lg" on:click={() => dispatch('back')}>Back</button>
  <div class="foot-r">
    <button class="ghost lg" on:click={() => run(usedAI)} disabled={running || !accountId}>{running ? 'Checking…' : 'Check again'}</button>
    <button class="primary lg" on:click={() => dispatch('new')}>Import another statement</button>
  </div>
</div>

<style>
  h1 { font-size: 26px; font-weight: 600; letter-spacing: -.01em; }
  .head { display: flex; flex-direction: column; gap: 6px; }
  .bal { flex-direction: row; align-items: center; gap: 10px; font-size: 14px; }
  .bal input { width: 150px; height: 34px; }
  .tiles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
  .grow { flex: 1; min-width: 0; }
  .checks { display: flex; flex-direction: column; }
  .lead { padding-bottom: 10px; font-size: 15px; }
  .check { display: flex; gap: 14px; align-items: flex-start; padding: 12px 0; border-top: 1px solid var(--border-soft); font-size: 14px; }
  .c-title { font-weight: 600; }
  .check.ok .c-icon { color: var(--accent2); }
  .check.warn .c-icon { color: var(--warn); }
  .check.bad .c-icon { color: var(--danger); }
  .check.bad .c-title { color: var(--danger); }
  .c-icon { padding-top: 2px; display: inline-flex; }
  .fixes { display: flex; gap: 16px; align-items: center; padding: 14px 18px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); font-size: 14px; }
  .ai { display: flex; gap: 12px; padding: 14px 18px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); font-size: 14px; line-height: 1.55; white-space: pre-wrap; }
  .ai .c-icon { color: var(--accent); }
  .ask { font-size: 14px; }
  .foot { margin-top: auto; display: flex; justify-content: space-between; align-items: center; padding-top: 8px; }
  .foot-r { display: flex; gap: 12px; }
</style>
