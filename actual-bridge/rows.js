// Statement row → Actual row happens once, in backend/money.py (to_actual): amounts arrive here
// as signed integer cents (money in positive), splits included, and are passed through as they are.
// The only thing left for the bridge is resolving the payee name to an existing payee id.
//
// row: { date, amount, imported_payee, payee_name, notes, cleared, imported_id?, legacy_ids?,
//        category?, subtransactions?: [{ amount, category?, notes }] }

export function isActualRow(r) {
  return r && typeof r.date === 'string' && Number.isInteger(r.amount) && !('is_credit' in r)
    && (r.subtransactions === undefined
      || (Array.isArray(r.subtransactions) && r.subtransactions.every(s => Number.isInteger(s.amount))));
}

export function buildActualTxn(row, payeeByName) {
  const { legacy_ids, payee_name, ...txn } = row;   // legacy_ids only feed the duplicate check
  const name = (payee_name || '').trim();
  const existingPayeeId = payeeByName.get(name.toLowerCase());
  if (existingPayeeId) txn.payee = existingPayeeId;
  else txn.payee_name = name;
  if (!txn.imported_id) delete txn.imported_id;
  return txn;
}
