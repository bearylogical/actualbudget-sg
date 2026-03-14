import express from 'express';
import * as api from '@actual-app/api';
import fs from 'fs';

const app = express();
app.use(express.json({ limit: '10mb' }));

const DATA_DIR = '/data/actual';
fs.mkdirSync(DATA_DIR, { recursive: true });

// Track which server we're currently connected to so we can detect credential changes
let initializedForURL = null;
let initializedForPassword = null;
let budgetLoaded = false;

// ── helpers ───────────────────────────────────────────────────────────────────

async function ensureInit(serverURL, password) {
  const credentialsChanged =
    serverURL !== initializedForURL || password !== initializedForPassword;

  if (credentialsChanged) {
    // Shut down any existing session before re-initialising
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
  if (!budgetLoaded) { res.status(400).json({ error: 'No budget loaded' }); return false; }
  return true;
}

// ── routes ────────────────────────────────────────────────────────────────────

app.get('/health', (_, res) => res.json({ ok: true }));

// List budgets on the server
app.post('/budgets', async (req, res) => {
  const { serverURL, password } = req.body;
  if (!serverURL || !password) {
    return res.status(400).json({ error: 'serverURL and password required' });
  }
  try {
    await ensureInit(serverURL, password);
    const budgets = await api.getBudgets();
    // getBudgets() returns objects with { id, groupId, name, ... }
    // downloadBudget() needs groupId, so expose both clearly
    res.json({ budgets });
  } catch (e) {
    // Reset so next attempt re-initialises cleanly
    try { await api.shutdown(); } catch (_) {}
    initializedForURL = null;
    initializedForPassword = null;
    res.status(500).json({ error: e.message });
  }
});

// Load a specific budget — uses groupId (not id) for downloadBudget
app.post('/budgets/load', async (req, res) => {
  const { serverURL, password, budgetId, encryptionPassword } = req.body;
  try {
    await ensureInit(serverURL, password);

    // budgetId here must be the groupId field from getBudgets()
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
    res.status(500).json({ error: e.message });
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
    res.status(500).json({ error: e.message });
  }
});

app.get('/categories', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ categoryGroups: await api.getCategoryGroups() });
  } catch (e) {
    res.status(500).json({ error: e.message });
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
    res.status(500).json({ error: e.message });
  }
});

app.get('/payees', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ payees: await api.getPayees() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/rules', async (_, res) => {
  if (!requireBudget(res)) return;
  try {
    res.json({ rules: await api.getRules() });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Create payee→category rules, fetching payees and existing rules once
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
    res.status(500).json({ error: e.message });
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
    res.status(500).json({ error: e.message });
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
    res.status(500).json({ error: e.message });
  }
});

app.post('/import', async (req, res) => {
  if (!requireBudget(res)) return;
  const { accountId, transactions, dryRun = false } = req.body;
  if (!accountId || !Array.isArray(transactions)) {
    return res.status(400).json({ error: 'accountId and transactions[] required' });
  }
  try {
    const existingPayees = await api.getPayees();
    const payeeByName = new Map(existingPayees.map(p => [p.name.toLowerCase().trim(), p.id]));

    const actualTxns = transactions.map(t => {
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
    });

    const result = await api.importTransactions(accountId, actualTxns);

    if (!dryRun) {
      await api.sync();
    }

    res.json({
      ok: true,
      dryRun,
      added: result.added?.length ?? 0,
      updated: result.updated?.length ?? 0,
      errors: result.errors ?? [],
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
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
