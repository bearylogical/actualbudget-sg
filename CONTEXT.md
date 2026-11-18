# budget-app

Turns Singapore bank exports into transactions in an Actual Budget account, keeps them categorised and reconciled, and surfaces anything doubtful for review.

## Language

### Importing

**Statement**:
One parsed bank export (UOB, DBS/POSB, OCBC): its rows, the period it covers, the balance it prints, and its account fingerprint.
_Avoid_: file, upload, export (for the parsed thing)

**Statement fingerprint**:
What identifies the bank account a Statement came from — bank, kind (deposit or credit card) and the last four digits.
_Avoid_: account key

**Import session**:
One Statement on its way into one Actual account: previewed, committed, and possibly undone. It is the same thing as the entry in import history.
_Avoid_: upload, import job

**Watch-folder import**:
An Import session that arrives through the watch folder and is committed with no decisions from a person.
_Avoid_: scheduler import, auto-import

**Wrong-account guard**:
The check that refuses to commit a Statement into an Actual account whose existing rows mostly belong to a different Statement fingerprint.
_Avoid_: cross-account conflict

**Legacy ID**:
An older form of a row's imported ID, kept so a Statement still matches rows imported before the ID scheme changed.
_Avoid_: old hash
