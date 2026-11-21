import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { classify } from '../dedup.js';
import { buildActualTxn, isActualRow } from '../rows.js';

// Shared with backend/tests/test_classify_golden.py, which runs the same cases against FakeActual.
const golden = JSON.parse(readFileSync(new URL('./fixtures/classify.json', import.meta.url)));

for (const c of golden.cases) {
  test(`classify: ${c.name}`, () => {
    const r = classify(c.incoming, c.existing);
    assert.deepEqual({
      clearly_new: r.clearlyNew.map(t => t.imported_id),
      clearly_dup: r.clearlyDup.map(t => t.imported_id),
      needs_verify: r.needsVerify.map(t => [t.imported_id, t.reason]),
    }, c.expect);
  });
}

const row = (extra = {}) => ({ date: '2026-09-05', amount: 3000, imported_payee: 'REFUND', payee_name: 'Shop',
  notes: '', cleared: true, imported_id: 'x', ...extra });

test('amounts and split lines pass through as signed cents', () => {
  const t = buildActualTxn(row({ legacy_ids: ['old'], subtransactions: [{ amount: 2000, notes: '' }, { amount: 1000, notes: '' }] }),
    new Map());
  assert.equal(t.amount, 3000);
  assert.deepEqual(t.subtransactions.map(s => s.amount), [2000, 1000]);
  assert.equal(t.payee_name, 'Shop');
  assert.ok(!('legacy_ids' in t));
});

test('a known payee name becomes its id', () => {
  const t = buildActualTxn(row(), new Map([['shop', 'p1']]));
  assert.equal(t.payee, 'p1');
  assert.ok(!('payee_name' in t));
});

test('old abs-amount + is_credit rows are refused, not guessed at', () => {
  assert.ok(isActualRow(row()));
  assert.ok(!isActualRow({ date: '2026-09-05', amount: 30, is_credit: true }));
  assert.ok(!isActualRow(row({ amount: 1.8 })));
  assert.ok(!isActualRow(row({ subtransactions: [{ amount: 0.5 }] })));
});
