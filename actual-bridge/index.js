import express from 'express';
import fs from 'fs';

// @actual-app/api is pre-installed in the actualbudget/actual-server base image
// Import path matches where it lives in that image
import * as api from '@actual-app/api';
import { classify } from './dedup.js';

const app = express();
app.use(express.json({ limit: '10mb' }));

const DATA_DIR = '/data/actual';
fs.mkdirSync(DATA_DIR, { recursive: true });

// ── Global safety net ────────────────────────────────────────────────────────
// The Actual API sometimes rejects promises with plain objects rather than
// Error instances. Without this handler Node crashes with ERR_UNHANDLED_REJECTION.
process.on('unhandledRejection', (reason) => {
  const msg = reason instanceof Error ? reason.message : JSON.stringify(reason);
  console.error('[unhandledRejection]', msg);
  // Do NOT exit — let express continue serving requests
});

// ── State ────────────────────────────────────────────────────────────────────
let initializedForURL = null;
let initializedForPassword = null;
let budgetLoaded = false;

// ── Helpers ──────────────────────────────────────────────────────────────────

// Normalise anything the API throws into a plain string
function errMsg(e) {
  if (typeof e === 'string') return e;
  if (e instanceof Error) return e.message;
  if (e && typeof e === 'object') {
    return e.message || e.reason || e.type || JSON.stringify(e);
  }
  return String(e);
}

async function ensureInit(serverURL, password) {
  const credentialsChanged =
    serverURL !== initializedForURL || password !== initializedForPassword;
  if (credentialsChanged) {
    if (initializedForURL !== null) {
      try { await api.shutdown(); } catch (_) {}
    }
    await api.init({ dataDir: DATA_DIR, serverURL, password });
    initializedForURL = serverURL;
    initializedForPassword = password;
    budgetLoaded = false;
  }
}

function toActualAmount(float) {
  return Math.round(float * 100);
}

function requireBudget(res) {
  if (!budgetLoaded) {
    res.status(400).json({ error: 'No budget loaded' });
    return false;
  }
  return true;
}

// ── Routes ───────────────────────────────────────────────────────────────────

// ── Health ───────────────────────────────────────────────────────────────────
// GET /health          cheap liveness (used by the Docker healthcheck)
// GET /health?deep=1   also pings the Actual server and compares versions
let apiVersion = null;
try {
  apiVersion = JSON.parse(fs.readFileSync(
    new URL('./node_modules/@actual-app/api/package.json', import.meta.url), 'utf8')).version;
} catch (_) {}
const startedAt = new Date().toISOString();

function majorMinor(v) {
  const m = String(v || '').match(/^(\d+)\.(\d+)/);
  return m ? `${m[1]}.${m[2]}` : null;
}

app.get('/health', async (req, res) => {
  const out = {
    ok: true,
    service: 'actual-bridge',
    startedAt,
    apiVersion,
    initialized: initializedForURL !== null,
    budgetLoaded,
    serverURL: initializedForURL,
  };
  if (req.query.deep && initializedForURL) {
    const t0 = Date.now();
    try {
      const r = await fetch(`${initializedForURL.replace(/\/$/, '')}/info`, { signal: AbortSignal.timeout(5000) });
      const body = await r.json().catch(() => ({}));
      const serverVersion = body?.build?.version ?? null;
      out.actualServer = {
        ok: r.ok,
        status: r.status,
        latencyMs: Date.now() - t0,
        version: serverVersion,
        // @actual-app/api must match the server release, or loads fail with out-of-sync-migrations
        versionMatch: serverVersion && apiVersion ? majorMinor(serverVersion) === majorMinor(apiVersion) : null,
      };
    } catch (e) {
      out.actualServer = { ok: false, latencyMs: Date.now() - t0, error: errMsg(e) };
    }
  }
  res.json(out);
});

app.post('/budgets', async (req, res) => {
  const { serverURL, password } = req.body;
  if (!serverURL || !password) {
    return res.status(400).json({ error: 'serverURL and password required' });
  }
  try {
    await ensureInit(serverURL, password);
    const budgets = await api.getBudgets();
    res.json({ budgets });
  } catch (e) {
    try { await api.shutdown(); } catch (_) {}
    initializedForURL = null;
    initializedForPassword = null;
    res.status(500).json({ error: errMsg(e) });
  }
});

app.post('/budgets/load', async (req, res) => {
  const { serverURL, password, budgetId, encryptionPassword } = req.body;
  try {
    await ensureInit(serverURL, password);

    // budgetId must be budget.groupId from getBudgets() — not budget.id
    if (encryptionPassword) {
      await api.downloadBudget(budgetId, { password: encryptionPassword });
    } else {
      await api.downloadBudget(budgetId);
    }
    budgetLoaded = true;

    const [accounts, categoryGroups, payees, rules] = await Promise.all([
      api.getAccounts(),
      api.getCategoryGroups(),
      api.getPayees(),
      api.getRules(),
    ]);

    const accountsWithBalance = await Promise.all(
      accounts.map(async a => {
        try { return { ...a, balance: await api.getAccountBalance(a.id) }; }
        catch { return { ...a, balance: null }; }
      })
    );

    res.json({ ok: true, accounts: accountsWithBalance, categoryGroups, payees, rules });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.get('/accounts', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    const accounts = await api.getAccounts();
    const accountsWithBalance = await Promise.all(
      accounts.map(async a => {
        try { return { ...a, balance: await api.getAccountBalance(a.id) }; }
        catch { return { ...a, balance: null }; }
      })
    );
    res.json({ accounts: accountsWithBalance });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.get('/categories', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ categoryGroups: await api.getCategoryGroups() });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.post('/categories', async (req, res) => {
  if (!requireBudget(res)) return;
  const { name, groupId, groupName } = req.body;
  try {
    let targetGroupId = groupId;
    if (!targetGroupId && groupName) {
      const groups = await api.getCategoryGroups();
      const existing = groups.find(g => g.name.toLowerCase() === groupName.toLowerCase());
      targetGroupId = existing ? existing.id : await api.createCategoryGroup({ name: groupName });
    }
    if (!targetGroupId) return res.status(400).json({ error: 'groupId or groupName required' });
    const id = await api.createCategory({ name, group_id: targetGroupId });
    await api.sync();
    res.json({ id, name, group_id: targetGroupId });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.get('/payees', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ payees: await api.getPayees() });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.get('/rules', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ rules: await api.getRules() });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.post('/rules', async (req, res) => {
  if (!requireBudget(res)) return;
  const { mappings } = req.body;
  if (!Array.isArray(mappings)) return res.status(400).json({ error: 'mappings[] required' });
  try {
    const [payees, existingRules] = await Promise.all([
      api.getPayees(),
      api.getRules(),
    ]);
    const payeeByName = new Map(payees.map(p => [p.name.toLowerCase(), p]));
    const existingPairs = new Set(
      existingRules.flatMap(r => {
        const catAction = r.actions?.find(a => a.field === 'category');
        const payeeCond = r.conditions?.find(c => c.field === 'payee');
        if (catAction && payeeCond) return [`${payeeCond.value}|${catAction.value}`];
        return [];
      })
    );
    const created = [];
    for (const { description, categoryId } of mappings) {
      const payee = payeeByName.get(description.toLowerCase());
      if (!payee || !categoryId) continue;
      if (existingPairs.has(`${payee.id}|${categoryId}`)) continue;
      const rule = await api.createRule({
        stage: null,
        conditionsOp: 'and',
        conditions: [{ field: 'payee', op: 'is', value: payee.id }],
        actions: [{ field: 'category', op: 'set', value: categoryId }],
      });
      created.push(rule);
      existingPairs.add(`${payee.id}|${categoryId}`);
    }
    await api.sync();
    res.json({ ok: true, created: created.length });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

// ── Full context for categorisation / rules audit ───────────────────────────
async function loadContext() {
  const [accounts, categoryGroups, payees, rules] = await Promise.all([
    api.getAccounts(), api.getCategoryGroups(), api.getPayees(), api.getRules(),
  ]);
  const categories = categoryGroups.flatMap(g => (g.categories || []).map(c => ({
    id: c.id, name: c.name, hidden: !!c.hidden,
    group_id: g.id, group_name: g.name, is_income: !!g.is_income,
  })));
  return { accounts, categoryGroups, categories, payees, rules };
}

app.get('/context', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json(await loadContext());
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

// Apply a sync plan produced by backend/rules_audit.py.
// Body: { createCategories: [{name, group, is_income}],
//         mergePayees: [{target, target_id, payees: [{id, name}]}],
//         createRules: [{stage, conditionsOp, conditions, actions}],   values may be
//                       {"$payee": name} (created if missing) or {"$category": name}
//         deleteRules: [ruleId] }
app.post('/rules/apply', async (req, res) => {
  if (!requireBudget(res)) return;
  const {
    createCategories = [], mergePayees = [], createRules = [], deleteRules = [],
  } = req.body || {};
  const result = { categories: 0, merged: 0, rules: 0, deleted: 0, errors: [] };
  try {
    let ctx = await loadContext();
    const groupByName = new Map(ctx.categoryGroups.map(g => [g.name.toLowerCase(), g]));
    const catByName = new Map(ctx.categories.map(c => [c.name.toLowerCase(), c.id]));
    const payeeByName = new Map(ctx.payees.map(p => [p.name.toLowerCase().trim(), p.id]));

    async function ensurePayee(name) {
      const key = name.toLowerCase().trim();
      if (payeeByName.has(key)) return payeeByName.get(key);
      const id = await api.createPayee({ name });
      payeeByName.set(key, id);
      return id;
    }

    // 1. categories
    for (const c of createCategories) {
      try {
        if (catByName.has(c.name.toLowerCase())) continue;
        let group = groupByName.get((c.group || 'Imported').toLowerCase());
        if (!group) {
          const gid = await api.createCategoryGroup({ name: c.group || 'Imported', is_income: !!c.is_income });
          group = { id: gid, name: c.group };
          groupByName.set((c.group || 'Imported').toLowerCase(), group);
        }
        const id = await api.createCategory({ name: c.name, group_id: group.id });
        catByName.set(c.name.toLowerCase(), id);
        result.categories++;
      } catch (e) { result.errors.push(`category ${c.name}: ${errMsg(e)}`); }
    }

    // 2. payee merges
    for (const m of mergePayees) {
      try {
        const targetId = m.target_id || await ensurePayee(m.target);
        const ids = (m.payees || []).map(p => p.id).filter(id => id && id !== targetId);
        if (!ids.length) continue;
        await api.mergePayees(targetId, ids);
        result.merged += ids.length;
      } catch (e) { result.errors.push(`merge → ${m.target}: ${errMsg(e)}`); }
    }

    // 3. rules
    async function resolveValue(v) {
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        if (v.$payee) return ensurePayee(v.$payee);
        if (v.$category) {
          const id = catByName.get(v.$category.toLowerCase());
          if (!id) throw new Error(`category "${v.$category}" not found`);
          return id;
        }
      }
      return v;
    }
    for (const r of createRules) {
      try {
        const conditions = [];
        for (const c of r.conditions || []) conditions.push({ field: c.field, op: c.op, value: await resolveValue(c.value) });
        const actions = [];
        for (const a of r.actions || []) actions.push({ field: a.field, op: a.op, value: await resolveValue(a.value) });
        await api.createRule({ stage: r.stage ?? null, conditionsOp: r.conditionsOp || 'and', conditions, actions });
        result.rules++;
      } catch (e) { result.errors.push(`rule (${r.why || 'unnamed'}): ${errMsg(e)}`); }
    }

    // 4. deletions
    for (const id of deleteRules) {
      try { await api.deleteRule(id); result.deleted++; }
      catch (e) { result.errors.push(`delete ${id}: ${errMsg(e)}`); }
    }

    await api.sync();
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: errMsg(e), ...result });
  }
});

// Which accounts already contain some of these transactions? Used by the
// backend's account recommender. Body: { ids: [imported ids], startDate, endDate }
app.post('/accounts/match', async (req, res) => {
  if (!requireBudget(res)) return;
  const { ids = [], startDate, endDate } = req.body || {};
  const idSet = new Set(ids);
  const pad = (iso, days) => {
    const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  };
  try {
    const accounts = await api.getAccounts();
    const out = [];
    for (const a of accounts) {
      if (a.closed) continue;
      let matchedIds = [];
      if (idSet.size && startDate && endDate) {
        const txns = await api.getTransactions(a.id, pad(startDate, -7), pad(endDate, 7));
        matchedIds = txns.map(t => t.imported_id).filter(id => id && idSet.has(id));
      }
      let balance = null;
      try { balance = await api.getAccountBalance(a.id); } catch (_) {}
      out.push({ id: a.id, name: a.name, offbudget: !!a.offbudget, closed: !!a.closed, balance, matchedIds });
    }
    res.json({ accounts: out });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

// ── Review-queue support: read, delete, link ────────────────────────────────
// All writes here are only ever called after a human approved them in the review UI.

// Every transaction on open accounts in a date range, flattened for analysis.
app.post('/txns/range', async (req, res) => {
  if (!requireBudget(res)) return;
  const { startDate, endDate, accountIds } = req.body || {};
  if (!startDate || !endDate) return res.status(400).json({ error: 'startDate and endDate required' });
  try {
    const [accounts, payees] = await Promise.all([api.getAccounts(), api.getPayees()]);
    const payeeById = new Map(payees.map(p => [p.id, p]));
    const out = [];
    for (const a of accounts) {
      if (a.closed || (accountIds && !accountIds.includes(a.id))) continue;
      for (const t of await api.getTransactions(a.id, startDate, endDate)) {
        const p = payeeById.get(t.payee);
        out.push({
          id: t.id, account: a.id, account_name: a.name, offbudget: !!a.offbudget,
          date: t.date, amount: t.amount, payee: p?.name ?? null, payee_id: t.payee ?? null,
          payee_transfer_acct: p?.transfer_acct ?? null,
          imported_payee: t.imported_payee ?? null, imported_id: t.imported_id ?? null,
          category: t.category ?? null, transfer_id: t.transfer_id ?? null, notes: t.notes ?? null,
          is_parent: !!t.is_parent, is_child: !!t.is_child, starting_balance_flag: !!t.starting_balance_flag,
        });
      }
    }
    res.json({ transactions: out, accounts: accounts.map(a => ({ id: a.id, name: a.name, offbudget: !!a.offbudget, closed: !!a.closed })) });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

// Delete transactions (approved duplicates). Deleted rows stay deleted on re-import
// because imports run with reimportDeleted=false.
app.post('/txns/delete', (req, res) => serialised(async () => {
  if (!requireBudget(res)) return;
  const { ids = [] } = req.body || {};
  const result = { deleted: 0, errors: [] };
  for (const id of ids) {
    try { await api.deleteTransaction(id); result.deleted++; }
    catch (e) { result.errors.push(`${id}: ${errMsg(e)}`); }
  }
  await api.sync();
  res.json({ ok: true, ...result });
}));

// Add plain transactions (approved reconciliation adjustments). No rules, no transfers.
app.post('/txns/add', (req, res) => serialised(async () => {
  if (!requireBudget(res)) return;
  const { accountId, transactions = [] } = req.body || {};
  if (!accountId || !transactions.length) return res.status(400).json({ error: 'accountId and transactions required' });
  try {
    const r = await api.addTransactions(accountId, transactions, { learnCategories: false, runTransfers: false });
    await api.sync();
    res.json({ ok: true, added: Array.isArray(r) ? r.length : transactions.length });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
}));

// Turn two separately imported rows into one Actual transfer.
// pairs: [{ keep_id, other_id }] — keep_id's account keeps its row; other_id is replaced
// by the transfer counterpart Actual creates. The deleted original's imported_id stays
// as a tombstone, so re-importing that statement doesn't bring it back.
app.post('/transfers/link', (req, res) => serialised(async () => {
  if (!requireBudget(res)) return;
  const { pairs = [] } = req.body || {};
  const result = { linked: 0, errors: [] };
  try {
    const payees = await api.getPayees();
    const accounts = await api.getAccounts();
    for (const { keep_id, other_id, keep_account, other_account, date } of pairs) {
      try {
        const window = [shift(date, -10), shift(date, 10)];
        const keepTx = (await api.getTransactions(keep_account, ...window)).find(t => t.id === keep_id);
        const otherTx = (await api.getTransactions(other_account, ...window)).find(t => t.id === other_id);
        if (!keepTx || !otherTx) throw new Error('transaction not found (already changed?)');
        if (keepTx.transfer_id || otherTx.transfer_id) throw new Error('already a transfer');
        if (keepTx.amount !== -otherTx.amount) throw new Error('amounts are not equal and opposite');
        const transferPayee = payees.find(p => p.transfer_acct === other_account);
        if (!transferPayee) throw new Error(`no transfer payee for account ${other_account}`);
        const onBudget = a => !accounts.find(x => x.id === a)?.offbudget;
        await api.deleteTransaction(other_id);
        // a transfer between two on-budget accounts carries no category
        await api.updateTransaction(keep_id, {
          payee: transferPayee.id,
          ...(onBudget(keep_account) && onBudget(other_account) ? { category: null } : {}),
        });
        await api.sync();
        // carry the bank reference over to the counterpart when Actual has created it
        const cp = (await api.getTransactions(other_account, ...window)).find(t => t.transfer_id === keep_id);
        if (cp) await api.updateTransaction(cp.id, { cleared: true, notes: otherTx.notes || cp.notes || null });
        result.linked++;
      } catch (e) {
        result.errors.push(`${keep_id} ↔ ${other_id}: ${errMsg(e)}`);
      }
    }
    await api.sync();
    res.json({ ok: true, ...result });
  } catch (e) {
    res.status(500).json({ error: errMsg(e), ...result });
  }
}));

function shift(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

app.post('/preview', async (req, res) => {
  if (!requireBudget(res)) return;
  const { accountId, startDate, endDate } = req.body;
  try {
    const existing = await api.getTransactions(accountId, startDate, endDate);
    const existingIds = new Set(existing.map(t => t.imported_id).filter(Boolean));
    res.json({ existingIds: [...existingIds], count: existing.length });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.get('/budget-month/:month', async (req, res) => {
  if (!requireBudget(res)) return;
  try {
    const [budget, categoryGroups] = await Promise.all([
      api.getBudgetMonth(req.params.month),
      api.getCategoryGroups(),
    ]);
    res.json({ budget, categoryGroups });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

function buildActualTxn(t, payeeByName) {
  // Clean payee name (backend/payees.py) so Actual's per-payee learning works;
  // the raw statement text is kept in imported_payee for rules and auditing.
  const payeeName = (t.payee || t.description || '').trim();
  const existingPayeeId = payeeByName.get(payeeName.toLowerCase());
  const txn = {
    date: t.date,
    amount: t.is_credit ? toActualAmount(t.amount) : -toActualAmount(t.amount),
    imported_payee: t.description,
    notes: t.notes || '',
    imported_id: t.imported_id || undefined,
    cleared: true,
    ...(t.category_id ? { category: t.category_id } : {}),
    ...(existingPayeeId ? { payee: existingPayeeId } : { payee_name: payeeName }),
  };
  if (Array.isArray(t.splits) && t.splits.length > 1) {
    txn.subtransactions = t.splits.map(s => ({
      amount: -toActualAmount(Number(s.amount) || 0),
      ...(s.category_id ? { category: s.category_id } : {}),
      notes: s.notes || '',
    }));
  }
  return txn;
}

// One import at a time: the scheduler and the UI share this bridge, and two
// concurrent imports of the same file could both pass the duplicate check.
let importQueue = Promise.resolve();
function serialised(fn) {
  const run = importQueue.then(fn, fn);
  importQueue = run.catch(() => {});
  return run;
}
// Deleted in Actual = stays deleted on re-import (Actual's default re-imports them).
const REIMPORT_DELETED = (process.env.REIMPORT_DELETED || 'false').toLowerCase() === 'true';

app.post('/import', (req, res) => serialised(() => importHandler(req, res)));

async function importHandler(req, res) {
  if (!requireBudget(res)) return;
  const { accountId, transactions, dryRun = false, verified = {}, reimportDeleted } = req.body;
  if (!accountId || !Array.isArray(transactions)) {
    return res.status(400).json({ error: 'accountId and transactions[] required' });
  }
  try {
    // Fetch existing transactions for the incoming date range (padded ±7 days) to
    // check for duplicates. The padding catches dupes that fall just outside a
    // re-imported statement window (e.g. a statement re-saved with the first row trimmed).
    const dates = transactions.map(t => t.date).sort();
    const shiftDate = (iso, days) => {
      const d = new Date(`${iso}T00:00:00Z`);
      d.setUTCDate(d.getUTCDate() + days);
      return d.toISOString().slice(0, 10);
    };
    const existing = await api.getTransactions(
      accountId,
      shiftDate(dates[0], -7),
      shiftDate(dates[dates.length - 1], 7),
    );
    const { clearlyNew, clearlyDup, needsVerify } = classify(transactions, existing);

    if (dryRun) {
      // Pure read-only preview — do not touch the budget
      return res.json({
        ok: true,
        dryRun: true,
        added: clearlyNew.length,
        skipped: clearlyDup.length,
        toVerify: needsVerify,
        updated: 0,
        errors: [],
      });
    }

    // For actual import: resolve needsVerify using user decisions from frontend
    const verifiedToImport = needsVerify.filter(t => verified[t.imported_id] === 'import');
    const toImport = [...clearlyNew, ...verifiedToImport];

    const existingPayees = await api.getPayees();
    const payeeByName = new Map(existingPayees.map(p => [p.name.toLowerCase().trim(), p.id]));
    const actualTxns = toImport.map(t => buildActualTxn(t, payeeByName));

    // Payees are already cleaned by the backend ("GrabFood", "SP Group", "AWS");
    // stop Actual from title-casing them into "Grabfood" / "Sp Group" / "Aws".
    const result = await api.importTransactions(accountId, actualTxns, {
      payeeNameNormalization: 'original',
      // an explicitly approved "import this missing row" may restore a deleted one
      reimportDeleted: typeof reimportDeleted === 'boolean' ? reimportDeleted : REIMPORT_DELETED,
    });
    await api.sync();

    const skipped = clearlyDup.length + needsVerify.filter(t => verified[t.imported_id] !== 'import').length;
    res.json({
      ok: true,
      dryRun: false,
      added: result.added?.length ?? 0,
      updated: result.updated?.length ?? 0,
      skipped,
      unreviewed: needsVerify.filter(t => verified[t.imported_id] !== 'import')
        .map(t => ({ date: t.date, description: t.description, amount: t.amount, reason: t.reason })),
      errors: result.errors ?? [],
    });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
}

app.post('/reset', async (_, res) => {
  try { await api.shutdown(); } catch (_) {}
  initializedForURL = null;
  initializedForPassword = null;
  budgetLoaded = false;
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`actual-bridge listening on :${PORT}`));
