import express from 'express';
import fs from 'fs';

// @actual-app/api is pre-installed in the actualbudget/actual-server base image
// Import path matches where it lives in that image
import * as api from '@actual-app/api';

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

app.get('/health', (_, res) => res.json({ ok: true }));

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
      targetGroupId = await api.createCategoryGroup({ name: groupName });
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
  const existingPayeeId = payeeByName.get(t.description.toLowerCase().trim());
  const txn = {
    date: t.date,
    amount: t.is_credit ? toActualAmount(t.amount) : -toActualAmount(t.amount),
    imported_payee: t.description,
    notes: t.notes || '',
    imported_id: t.imported_id || undefined,
    cleared: true,
    ...(t.category_id ? { category: t.category_id } : {}),
    ...(existingPayeeId ? { payee: existingPayeeId } : { payee_name: t.description }),
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

app.post('/import', async (req, res) => {
  if (!requireBudget(res)) return;
  const { accountId, transactions, dryRun = false, verified = {} } = req.body;
  if (!accountId || !Array.isArray(transactions)) {
    return res.status(400).json({ error: 'accountId and transactions[] required' });
  }
  try {
    // Fetch existing transactions for the incoming date range to check for duplicates
    const dates = transactions.map(t => t.date).sort();
    const existing = await api.getTransactions(accountId, dates[0], dates[dates.length - 1]);
    const existingIds = new Set(existing.map(t => t.imported_id).filter(Boolean));

    // Classify into three buckets
    const clearlyNew = [];
    const clearlyDup = [];
    const needsVerify = [];

    for (const t of transactions) {
      const hasRef = t.imported_id && t.imported_id.startsWith('ref-');
      const idMatch = existingIds.has(t.imported_id) || existingIds.has(t.legacy_id);

      if (!idMatch) {
        clearlyNew.push(t);
      } else if (hasRef) {
        clearlyDup.push(t);   // bank-assigned ref confirms it's a duplicate
      } else {
        needsVerify.push(t);  // hash collision — may be a legitimate second transaction
      }
    }

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

    const result = await api.importTransactions(accountId, actualTxns);
    await api.sync();

    const skipped = clearlyDup.length + needsVerify.filter(t => verified[t.imported_id] !== 'import').length;
    res.json({
      ok: true,
      dryRun: false,
      added: result.added?.length ?? 0,
      updated: result.updated?.length ?? 0,
      skipped,
      errors: result.errors ?? [],
    });
  } catch (e) {
    res.status(500).json({ error: errMsg(e) });
  }
});

app.post('/reset', async (_, res) => {
  try { await api.shutdown(); } catch (_) {}
  initializedForURL = null;
  initializedForPassword = null;
  budgetLoaded = false;
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`actual-bridge listening on :${PORT}`));
