// Duplicate classification for /import — pure function so it can be unit-tested
// (`node --test`). Actual's own importer only fuzzy-matches rows that have NO
// imported_id (strictIdChecking), so any change of id between two imports of the
// same transaction (xls vs PDF export, old vs new id scheme) would otherwise be
// silently added twice. This closes that gap.
//
// incoming: [{ imported_id, legacy_ids?, legacy_id?, date, amount, is_credit }]  (amount = abs SGD)
// existing: [{ id, imported_id, date, amount }]                                   (amount = signed cents)
//
// → { clearlyNew, clearlyDup, needsVerify }   needsVerify rows carry `reason`
//
// Rules
//  1. id match (imported_id or any legacy id) against an existing row → duplicate.
//     Each existing row can be claimed once, so two identical coffees imported
//     earlier still pair up 1:1 with two identical coffees now.
//  2. no id match, but an UNCLAIMED existing row with an imported_id has the same
//     date and amount, inside this statement's date range → needs review
//     ("already imported under a different id", e.g. same statement as PDF).
//     Rows with no imported_id (typed in by hand) are left to Actual, which links them.
//  3. everything else → new.

export function signedCents(t) {
  const c = Math.round(Number(t.amount) * 100);
  return t.is_credit ? c : -c;
}

export function classify(incoming, existing) {
  const byId = new Map();
  for (const e of existing) {
    if (!e.imported_id) continue;
    if (!byId.has(e.imported_id)) byId.set(e.imported_id, []);
    byId.get(e.imported_id).push(e);
  }
  const claimed = new Set();
  const clearlyNew = [], clearlyDup = [], needsVerify = [], unmatched = [];

  for (const t of incoming) {
    const legacy = Array.isArray(t.legacy_ids) ? t.legacy_ids : (t.legacy_id ? [t.legacy_id] : []);
    let hit = null;
    for (const id of [t.imported_id, ...legacy]) {
      if (!id) continue;
      hit = (byId.get(id) || []).find(e => !claimed.has(e.id));
      if (hit) break;
    }
    if (hit) { claimed.add(hit.id); clearlyDup.push(t); }
    else unmatched.push(t);
  }

  const dates = incoming.map(t => t.date).filter(Boolean).sort();
  const lo = dates[0], hi = dates[dates.length - 1];
  for (const t of unmatched) {
    const cents = signedCents(t);
    const twin = existing.find(e =>
      e.imported_id && !claimed.has(e.id) &&
      e.date === t.date && e.date >= lo && e.date <= hi && e.amount === cents);
    if (twin) {
      claimed.add(twin.id);
      needsVerify.push({ ...t, reason: `same date and amount already imported (id ${String(twin.imported_id).slice(0, 24)})` });
    } else {
      clearlyNew.push(t);
    }
  }
  return { clearlyNew, clearlyDup, needsVerify };
}
