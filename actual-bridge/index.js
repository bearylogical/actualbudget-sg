import express from 'express';
import * as api from '@actual-app/api';
import fs from 'fs';

const app = express();
app.use(express.json({ limit: '10mb' }));

const DATA_DIR = '/data/actual';
fs.mkdirSync(DATA_DIR, { recursive: true });

let initialized = false;
let budgetLoaded = false;

// ── helpers ───────────────────────────────────────────────────────────────────

async function ensureInit(serverURL, password) {
  if (!initialized) {
    await api.init({ dataDir: DATA_DIR, serverURL, password });
    initialized = true;
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

app.post('/budgets', async (req, res) => {
  const { serverURL, password } = req.body;
  try {
    await ensureInit(serverURL, password);
    res.json({ budgets: await api.getBudgets() });
  } catch (e) {
    initialized = false;
    res.status(500).json({ error: e.message });
  }
});

app.post('/budgets/load', async (req, res) => {
  const { serverURL, password, budgetId, encryptionPassword } = req.body;
  try {
    await ensureInit(serverURL, password);
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

// Create payee→category rules. Fetches payees and existing rules once, then
// processes all mappings in a single pass (no N+1 queries).
app.post('/rules', async (req, res) => {
  if (!requireBudget(res)) return;
  const { mappings } = req.body; // [{ description, categoryId }]
  if (!Array.isArray(mappings)) return res.status(400).json({ error: 'mappings[] required' });
  try {
    // Fetch payees and existing rules once before the loop
    const [payees, existingRules] = await Promise.all([
      api.getPayees(),
      api.getRules(),
    ]);

    const payeeByName = new Map(payees.map(p => [p.name.toLowerCase(), p]));

    // Build a set of existing payeeId→categoryId pairs to avoid duplicates
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
      existingPairs.add(`${payee.id}|${categoryId}`); // prevent dupes within same batch
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
  const { accountId, transactions, learnCategories = false, dryRun = false } = req.body;
  if (!accountId || !Array.isArray(transactions)) {
    return res.status(400).json({ error: 'accountId and transactions[] required' });
  }
  try {
    // Resolve existing payees once to avoid creating duplicates
    const existingPayees = await api.getPayees();
    const payeeByName = new Map(existingPayees.map(p => [p.name.toLowerCase().trim(), p.id]));

    const actualTxns = transactions.map(t => {
      const descLower = t.description.toLowerCase().trim();
      const existingPayeeId = payeeByName.get(descLower);

      const txn = {
        date: t.date,
        amount: t.is_credit ? toActualAmount(t.amount) : -toActualAmount(t.amount),
        imported_payee: t.description,
        notes: t.notes || '',
        imported_id: t.imported_id || undefined,
        cleared: true,
        ...(t.category_id ? { category: t.category_id } : {}),
        ...(existingPayeeId
          ? { payee: existingPayeeId }
          : { payee_name: t.description }),
      };

      // Map split lines to Actual subtransactions
      if (Array.isArray(t.splits) && t.splits.length > 1) {
        txn.subtransactions = t.splits.map(s => ({
          amount: -toActualAmount(Number(s.amount) || 0),
          ...(s.category_id ? { category: s.category_id } : {}),
          notes: s.notes || '',
        }));
      }

      return txn;
    });

    // importTransactions signature: (accountId, transactions) — no options object
    // learnCategories is handled via addTransactions flag, not importTransactions
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
  try { if (initialized) await api.shutdown(); } catch (_) {}
  initialized = false;
  budgetLoaded = false;
  res.json({ ok: true });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => console.log(`actual-bridge listening on :${PORT}`));
