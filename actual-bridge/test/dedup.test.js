import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify } from '../dedup.js';

const inc = (id, date, amount, extra = {}) => ({ imported_id: id, date, amount, is_credit: false, ...extra });
const ex = (id, imported_id, date, cents) => ({ id, imported_id, date, amount: cents });

test('bank-ref and legacy-id matches are duplicates', () => {
  const r = classify(
    [inc('ref-1', '2026-09-05', 1.8), inc('ref-2', '2026-09-05', 5, { legacy_ids: ['h2'] })],
    [ex('a', 'ref-1', '2026-09-05', -180), ex('b', 'h2', '2026-09-05', -500)]);
  assert.equal(r.clearlyDup.length, 2);
  assert.equal(r.clearlyNew.length, 0);
});

test('two identical coffees pair 1:1 with two already imported, a third is new', () => {
  const existing = [ex('a', 'h', '2026-09-05', -180), ex('b', 'h', '2026-09-05', -180)];
  const r = classify([
    inc('h', '2026-09-05', 1.8),
    inc('h-1', '2026-09-05', 1.8, { legacy_ids: ['h'] }),
    inc('h-2', '2026-09-05', 1.8, { legacy_ids: ['h'] }),
  ], existing);
  assert.equal(r.clearlyDup.length, 2);
  assert.equal(r.clearlyNew.length, 1);
  assert.equal(r.needsVerify.length, 0);
});

test('same txn under a different id (xls vs PDF) is held for review, not added', () => {
  const r = classify([inc('pdf-hash', '2026-09-05', 8.5)], [ex('a', 'xls-hash', '2026-09-05', -850)]);
  assert.equal(r.needsVerify.length, 1);
  assert.match(r.needsVerify[0].reason, /already imported/);
});

test('hand-entered rows (no imported_id) are left to Actual to link', () => {
  const r = classify([inc('x', '2026-09-05', 8.5)], [ex('a', null, '2026-09-05', -850)]);
  assert.equal(r.clearlyNew.length, 1);
});

test('existing rows outside the statement range never trigger review', () => {
  const r = classify([inc('x', '2026-09-05', 8.5), inc('y', '2026-09-10', 1)],
    [ex('a', 'old', '2026-09-01', -850)]);
  assert.equal(r.clearlyNew.length, 2);
});

test('credits compare with positive cents', () => {
  const r = classify([inc('x', '2026-09-05', 100, { is_credit: true })], [ex('a', 'other', '2026-09-05', 10000)]);
  assert.equal(r.needsVerify.length, 1);
});
