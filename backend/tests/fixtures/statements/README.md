# Statement fixtures

Synthetic statements, one per supported layout, used by `test_parser_golden.py`.
Every name, account number and reference here is made up — never commit a real
statement. Copy the *layout* of a real export (headers, column order, line
wrapping, summary rows) and replace the contents.

`*.txt` files stand in for PDF statements: they are what `pdf_text()` returns
(pypdf layout mode), so column alignment matters.

Expected output lives in `../golden/<fixture>.json`. After an intentional parser
change, regenerate and review the diff:

    UPDATE_GOLDEN=1 python -m pytest -q tests/test_parser_golden.py
    git diff tests/fixtures/golden

A changed `imported_id` or `legacy_ids` means rows already imported into Actual
will no longer match and would be imported again — keep the old id as a legacy id.
