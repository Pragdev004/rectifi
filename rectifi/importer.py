"""Bank statement import: CSV/Excel/PDF -> normalised rows -> deduped, merged, reconciled transactions.

Three steps you can see separately in the UI:
  1. read_table()     raw file -> DataFrame (finds the header row under the bank's letterhead).
                      A PDF is first turned into a table by rectifi/pdf_statement.py; after that it is treated like any CSV.
  2. detect_columns() which column is the date / narration / debit / credit / balance
  3. import_rows()    write to Postgres with dedupe + merging + balance snapshots
assess_statement() decides whether steps 1 and 2 were clear enough to import without asking (the Add page then imports
at once, and the batch can be undone). If anything is unclear, the person sees the manual preview instead.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from . import categorize
from .db import connect
from .money import parse_amount
from .parsing import narration_details
from .pdf_statement import NO_ROWS_MESSAGE, PdfContent, describe_review, join_wrapped, read_pdf, table_from_text
from .tools import RectifiError, _account_id, _categorize_detail, _suggestion_id, resolve_date

ROLES = ["date", "narration", "debit", "credit", "amount", "type", "balance", "ref"]

KEYWORDS = {
    "date": ["txn date", "transaction date", "tran date", "value date", "date", "posting date"],
    "narration": ["narration", "description", "particulars", "remarks", "details", "transaction details", "transaction remarks"],
    "debit": ["withdrawal amt", "withdrawal amount", "withdrawal", "withdrawals", "debit amount", "debit", "dr amount", "dr"],
    "credit": ["deposit amt", "deposit amount", "deposit", "deposits", "credit amount", "credit", "cr amount", "cr"],
    "amount": ["amount", "transaction amount", "txn amount", "amount (inr)", "amount(inr)"],
    "type": ["dr/cr", "cr/dr", "type", "debit/credit", "transaction type"],
    "balance": ["closing balance", "balance", "available balance", "balance (inr)", "running balance"],
    "ref": ["chq/ref no", "chq./ref.no.", "ref no", "reference no", "cheque no", "chq no", "utr", "ref no./cheque no.", "transaction id"],
}


@dataclass
class StatementRow:
    txn_date: date
    amount: Decimal
    direction: str
    narration: str
    balance: Decimal | None
    ref: str | None
    line: int
    review: str | None = None        # set when money in vs out could not be proven: such a row is never imported by guess


@dataclass
class ImportResult:
    added: int = 0
    duplicates: int = 0
    merged: int = 0
    skipped: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    opening: tuple[date, Decimal] | None = None
    closing: tuple[date, Decimal] | None = None
    batch_id: int | None = None          # import_batches.id, so the import can be undone
    account_id: int | None = None
    account_name: str | None = None


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower()).strip(" .:")


def _looks_like_header(cells) -> bool:
    cells = [_norm(c) for c in cells if pd.notna(c) and str(c).strip()]
    has_date = any("date" in c for c in cells)
    has_text = any(any(k in c for k in ("narration", "description", "particulars", "remarks", "details")) for c in cells)
    return has_date and has_text


def read_table(data: bytes, filename: str, password: str | None = None) -> pd.DataFrame:
    """Read CSV/XLS(X)/PDF and locate the real header row (statements start with address/letterhead rows).

    `password` is only used for a PDF. It is passed straight to the PDF reader and never kept."""
    name = filename.lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        content = read_pdf(data, password)
        table = pdf_to_table(content)
        table.attrs["account_last4"] = _last4_from_lines(_lines_before_first_date(content.pages[0].lines if content.pages else []))
        return table
    if name.endswith((".xlsx", ".xls")):
        try:
            raw = pd.read_excel(io.BytesIO(data), header=None, dtype=str)
        except ImportError as exc:    # pandas needs the extra package "xlrd" for the old .xls format
            raise RectifiError(
                "Rectifi can't open old .xls files. Open the file in Excel (or Google Sheets), "
                "choose Save As, pick .xlsx or CSV, and upload that instead."
            ) from exc
        except (ValueError, zipfile.BadZipFile) as exc:
            raise RectifiError(f"Couldn't read {filename} as an Excel file. Try saving it as CSV and uploading that.") from exc
    else:
        text = data.decode("utf-8-sig", errors="replace")
        raw = pd.read_csv(io.StringIO(text), header=None, dtype=str, sep=None, engine="python",
                          skip_blank_lines=True, on_bad_lines="skip")
    raw = raw.dropna(how="all")
    header_index = None
    for idx, row in raw.head(40).iterrows():
        if _looks_like_header(row):
            header_index = idx
            break
    if header_index is None:
        raise RectifiError("Couldn't find the header row — I need columns like Date and Narration/Description.")
    header = [(_norm(c) if pd.notna(c) else f"col{i}") for i, c in enumerate(raw.loc[header_index])]
    letterhead = [" ".join(str(c) for c in row if pd.notna(c)) for row in raw.loc[:header_index].iloc[:-1].itertuples(index=False)]
    table = raw.loc[header_index + 1:].copy() if isinstance(header_index, int) else raw.iloc[1:].copy()
    table.columns = header
    table = table.dropna(how="all").reset_index(drop=True)
    table.attrs["account_last4"] = _last4_from_lines(letterhead)        # set last: some pandas operations drop attrs
    return table


# ───────────────────────── which account is this statement for? ─────────────────────────

_ACCOUNT_LINE = re.compile(
    r"(?i)\b(?:a/?c|account)\s*(?:no\.?|number|num\.?|#)?\s*[:\-]?\s*((?:[x*#]|\d)[x*#\d\s-]{2,}\d{4})(?!\d)"
)


def _last4_from_lines(lines: list[str]) -> str | None:
    """The last 4 digits of the account number printed in a statement's letterhead ('Account No : XXXXXXXX4417'),
    or None if there is none or the letterhead names more than one different account."""
    found = set()
    for line in lines:
        for match in _ACCOUNT_LINE.finditer(str(line)):
            found.add(re.sub(r"\D", "", match.group(1))[-4:])
    return found.pop() if len(found) == 1 else None


def _lines_before_first_date(lines: list[str]) -> list[str]:
    """The first lines of a PDF page, up to the first line that starts with a date (after that it is transactions)."""
    from .pdf_statement import DATE_AT_START

    head = []
    for line in lines[:40]:
        if DATE_AT_START.match(line):
            break
        head.append(line)
    return head


def statement_account_last4(table: pd.DataFrame) -> str | None:
    """The account digits read from the statement's letterhead by read_table, if it printed any."""
    return table.attrs.get("account_last4")


# ───────────────────────── PDF -> table ─────────────────────────

_HAS_MONEY = re.compile(r"\d[\d,]*\.\d{2}")


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


def _table_from_pdf_tables(content: PdfContent) -> pd.DataFrame | None:
    """Every ruled table on every page -> one table. The header may repeat on each page (or only be on page 1);
    tables that are not the transaction table (a summary box, say) are left out because their shape differs."""
    header: list[str] | None = None
    body: list[list[str]] = []
    for page in content.pages:
        for table in page.tables:
            if not table or not table[0]:
                continue
            at = next((i for i, row in enumerate(table[:6]) if _looks_like_header(row)), None)
            if at is not None:
                if header is None:
                    header = [(_norm(c) if c.strip() else f"col{i}") for i, c in enumerate(table[at])]
                if len(table[at]) != len(header):
                    continue
                body.extend(row for row in table[at + 1:] if len(row) == len(header))
            elif header is not None and len(table[0]) == len(header):
                body.extend(table)               # a later page that does not repeat the header
    if header is None:
        return None
    normal = [_norm(h) for h in header]
    body = [row for row in body if [_norm(c) for c in row] != normal]       # a header repeated in the middle
    if not body:
        return None
    return pd.DataFrame(body, columns=_unique(header), dtype=object)


def _tidy_pdf_table(table: pd.DataFrame) -> pd.DataFrame:
    """Fix what a PDF table does that a CSV does not:
    blank separator rows go; a row with no date and no money is the rest of the row above (wrapped narration);
    a row with money but no date takes the date of the row above (some banks print each date once)."""
    mapping = detect_columns(table)
    date_col, narration_col = mapping.get("date"), mapping.get("narration")
    money_cols = [c for c in (mapping.get("debit"), mapping.get("credit"), mapping.get("amount"), mapping.get("balance")) if c]
    if not date_col or not narration_col or not money_cols:
        return table
    out: list[dict] = []
    for record in table.to_dict("records"):
        values = {k: ("" if pd.isna(v) else str(v).strip()) for k, v in record.items()}
        if not any(values.values()):
            continue
        has_money = any(_HAS_MONEY.search(values[c]) for c in money_cols)
        if not values[date_col]:
            if not has_money and out:
                for column, text in values.items():
                    if text:
                        out[-1][column] = join_wrapped(out[-1][column], text) if out[-1][column] else text
                continue
            if has_money and out:
                values[date_col] = out[-1][date_col]
        out.append(values)
    return pd.DataFrame(out, columns=list(table.columns), dtype=object)


def pdf_to_table(content: PdfContent) -> pd.DataFrame:
    """The PDF's tables if it has a transaction table, otherwise the text-line reader. Raises if there is nothing."""
    table = _table_from_pdf_tables(content)
    if table is not None:
        table = _tidy_pdf_table(table)
        if len(table) and detect_columns(table).get("date"):
            return table.reset_index(drop=True)
    text_table, _ = table_from_text([page.lines for page in content.pages])
    if text_table.empty:
        raise RectifiError(NO_ROWS_MESSAGE)
    return text_table


def detect_columns(table: pd.DataFrame) -> dict[str, str | None]:
    """Best-guess column for each role. Exact keyword match beats 'contains'."""
    columns = list(table.columns)
    mapping: dict[str, str | None] = {}
    used: set[str] = set()
    for role in ["date", "narration", "balance", "debit", "credit", "type", "ref", "amount"]:
        choice = None
        for keyword in KEYWORDS[role]:
            exact = [c for c in columns if c == keyword and c not in used]
            if exact:
                choice = exact[0]
                break
        if choice is None:
            for keyword in KEYWORDS[role]:
                partial = [c for c in columns if keyword in c and c not in used and len(keyword) > 2]
                if partial:
                    choice = partial[0]
                    break
        mapping[role] = choice
        if choice:
            used.add(choice)
    if mapping["debit"] and mapping["credit"]:
        mapping["amount"] = None
        mapping["type"] = None
    return mapping


def _amount_or_none(value) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    text = str(value).strip()
    if text in ("", "-", "--", "nan", "None", "0", "0.00"):
        return None
    negative = text.startswith("-") or text.startswith("(")
    text = text.strip("()-").replace("Cr", "").replace("Dr", "").replace("CR", "").replace("DR", "")
    amount = parse_amount(text)
    return -amount if negative else amount


def normalise(table: pd.DataFrame, mapping: dict[str, str | None]) -> tuple[list[StatementRow], list[str]]:
    """Turn the bank's columns into StatementRows in chronological order."""
    if not mapping.get("date") or not mapping.get("narration"):
        raise RectifiError("Map at least the Date and Narration columns.")
    if not ((mapping.get("debit") and mapping.get("credit")) or mapping.get("amount")):
        raise RectifiError("Map either Debit + Credit columns, or a single Amount column.")
    rows: list[StatementRow] = []
    problems: list[str] = []
    for i, record in table.iterrows():
        line = int(i) + 1
        raw_date = record.get(mapping["date"])
        if pd.isna(raw_date) or not str(raw_date).strip():
            continue
        try:
            when = pd.to_datetime(str(raw_date).strip(), dayfirst=True, errors="raise").date()
        except (ValueError, TypeError):
            problems.append(f"Row {line}: unreadable date {raw_date!r} — skipped (often a footer line).")
            continue
        reason = str(record.get("__review") or "").strip() if "__review" in table.columns else ""
        if reason and reason != "nan":
            try:
                rows.append(StatementRow(when, abs(_amount_or_none(record.get("__amount")) or Decimal(0)), "",
                                         str(record.get(mapping["narration"]) or "").strip(), None, None, line, review=reason))
            except ValueError as exc:
                problems.append(f"Row {line}: {exc} — skipped.")
            continue
        try:
            if mapping.get("debit") and mapping.get("credit"):
                debit = _amount_or_none(record.get(mapping["debit"]))
                credit = _amount_or_none(record.get(mapping["credit"]))
                if debit and debit != 0:
                    amount, direction = abs(debit), "debit"
                elif credit and credit != 0:
                    amount, direction = abs(credit), "credit"
                else:
                    continue
            else:
                value = _amount_or_none(record.get(mapping["amount"]))
                if value is None or value == 0:
                    continue
                kind = _norm(record.get(mapping["type"])) if mapping.get("type") else ""
                if kind.startswith(("dr", "debit", "d")) and kind:
                    direction = "debit"
                elif kind.startswith(("cr", "credit", "c")) and kind:
                    direction = "credit"
                else:
                    direction = "debit" if value < 0 else "credit"
                amount = abs(value)
            balance = _amount_or_none(record.get(mapping["balance"])) if mapping.get("balance") else None
        except ValueError as exc:
            problems.append(f"Row {line}: {exc} — skipped.")
            continue
        ref = record.get(mapping["ref"]) if mapping.get("ref") else None
        ref = str(ref).strip() if ref is not None and pd.notna(ref) and str(ref).strip() not in ("", "nan", "-") else None
        rows.append(StatementRow(when, amount, direction, str(record.get(mapping["narration"]) or "").strip(), balance, ref, line))
    if len(rows) >= 2 and rows[0].txn_date > rows[-1].txn_date:
        rows.reverse()  # newest-first statements
    return rows, problems


@dataclass
class StatementCheck:
    """Can this statement be imported without asking? `confident` says yes; otherwise `reasons` say what is unclear
    and the Add page shows the manual preview with the column choices instead of guessing."""
    mapping: dict[str, str | None]
    rows: list[StatementRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    confident: bool = False
    reasons: list[str] = field(default_factory=list)
    account_last4: str | None = None


MAX_UNREADABLE_SHARE = 0.2        # more than this share of lines unreadable: something is wrong with the columns
MAX_UNREADABLE_FREE = 3           # ...but a few footer lines ("Total", "Computer generated statement") are always fine
_MONTH_FIRST = re.compile(r"^\s*\d{1,2}[/.-](\d{1,2})[/.-]\d{2,4}\s*$")
_DIRECTION_WORD = re.compile(r"(?i)^(dr|cr|debit|credit|d|c)\b")


def _column(table: pd.DataFrame, name: str) -> list:
    """The non-empty values of one column (the first one, if the bank repeated a header name)."""
    values = table.iloc[:, list(table.columns).index(name)]
    return [v for v in values if pd.notna(v) and str(v).strip()]


def assess_statement(table: pd.DataFrame) -> StatementCheck:
    """Decide whether the columns are clear enough to import at once. It never guesses:

    - a Date, a Narration and either Debit + Credit columns or one Amount column must all be found
    - one Amount column needs a way to tell money in from money out (a Dr/Cr column, or minus signs)
    - dates must not look month-first (03/25/26): Rectifi reads them day-first
    - at most one line in five may be unreadable (up to 3 footer lines are always fine)
    - at least one row must be importable (a PDF row whose direction is unproven never is)
    """
    mapping = detect_columns(table)
    check = StatementCheck(mapping=mapping, account_last4=statement_account_last4(table))
    why = check.reasons
    if not mapping.get("date"):
        why.append("I couldn't tell which column holds the date.")
    if not mapping.get("narration"):
        why.append("I couldn't tell which column holds the description.")
    split = bool(mapping.get("debit") and mapping.get("credit"))
    if not split and not mapping.get("amount"):
        if mapping.get("debit") or mapping.get("credit"):
            why.append("I found only a Debit or only a Credit column. I need both, or one Amount column.")
        else:
            why.append("I couldn't find the money columns (Debit and Credit, or one Amount).")
    if why:
        return check

    if mapping.get("amount") and not split:
        values = []
        for v in _column(table, mapping["amount"]):
            try:
                values.append(_amount_or_none(v))
            except ValueError:
                values.append(None)              # normalise() will report this row as unreadable
        has_minus = any(v is not None and v < 0 for v in values)
        kinds = [_norm(v) for v in _column(table, mapping["type"])] if mapping.get("type") else []
        if kinds and not all(_DIRECTION_WORD.match(k) for k in kinds) and not has_minus:
            why.append("The Type column has words other than Debit/Credit (Dr/Cr), so I can't tell money in from money out.")
        elif not kinds and not has_minus:
            why.append("There is one Amount column with no minus signs and no Debit/Credit marker, "
                       "so I can't tell money in from money out.")
    for raw in _column(table, mapping["date"]):
        found = _MONTH_FIRST.match(str(raw))
        if found and int(found.group(1)) > 12:
            why.append("The dates look month-first (like 03/25/26). Rectifi reads day-first, so check them before importing.")
            break
    if why:
        return check

    try:
        rows, problems = normalise(table, mapping)
    except RectifiError as exc:
        why.append(str(exc))
        return check
    check.rows, check.problems = rows, problems
    candidates = len(rows) + len(problems)
    if not rows:
        why.append("No transactions were found with these columns.")
    elif not any(not r.review for r in rows):
        why.append("Every row needs review: Rectifi can't prove from the balance whether the money went out or came in.")
    elif len(problems) > max(MAX_UNREADABLE_FREE, MAX_UNREADABLE_SHARE * candidates):
        why.append(f"{len(problems)} of {candidates} lines couldn't be read (dates or amounts), which is too many to trust the column choice.")
    check.confident = not why
    return check


def preview_totals(rows: list[StatementRow]) -> dict:
    """The sums the import preview shows: row count, money out, money in, first and last date (None when empty).
    `rows` are the rows that would be imported (review rows already taken out)."""
    out = sum((r.amount for r in rows if r.direction == "debit"), Decimal("0.00"))
    inn = sum((r.amount for r in rows if r.direction == "credit"), Decimal("0.00"))
    return {"count": len(rows), "money_out": out, "money_in": inn,
            "first": rows[0].txn_date if rows else None, "last": rows[-1].txn_date if rows else None}


def _fingerprint(account_id: int, row: StatementRow, occurrence: int = 1) -> str:
    """A hash that identifies one statement row. `occurrence` is 1 for the first row with these exact details
    in a file, 2 for the second, and so on. Two identical rows on one day (say, two ₹20 chai payments with no
    balance column) are two real payments, so they must get two different fingerprints. Occurrence 1 hashes
    exactly what older versions hashed, so statements imported before this change still count as already imported."""
    key = f"{account_id}|{row.txn_date}|{row.amount}|{row.direction}|{row.narration}|{row.balance}|{row.ref}"
    if occurrence > 1:
        key += f"|#{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def import_rows(rows: list[StatementRow], account: str | int, filename: str | None = None) -> ImportResult:
    """Write statement rows. For each row, in order:
        1. same UPI ref already stored       -> duplicate (and attach the account if it was missing)
        2. same row fingerprint already stored -> duplicate (you imported this file before)
        3. a chat/SMS/manual entry with the same amount, direction and date ±2 days and no ref
                                              -> MERGE: it's the same payment, now confirmed by the bank
        4. otherwise                          -> new transaction, categorised by merchant rules
    Then the first and last running balances become reconciliation snapshots.

    Every call that has rows to write creates one import_batches row, and each new transaction carries its id
    (transactions.import_batch_id). That is what lets tools.undo_import remove exactly what this import added.
    Merged and duplicate rows are not tagged, so undo never touches them.
    """
    result = ImportResult()
    result.needs_review = [describe_review(r.amount, r.narration, r.review) for r in rows if r.review]
    rows = [r for r in rows if not r.review]        # a row whose direction is unproven is never imported by guess
    if not rows:
        return result
    with connect() as conn:
        account_id = _account_id(conn, account)
        if account_id is None:
            raise RectifiError("Choose which account this statement belongs to.")
        named = conn.execute("SELECT name FROM accounts WHERE id = %s", (account_id,)).fetchone()
        result.account_id, result.account_name = account_id, named["name"] if named else None
        batch_id = conn.execute(
            "INSERT INTO import_batches (account_id, filename) VALUES (%s, %s) RETURNING id",
            (account_id, (filename or "")[:200] or None),
        ).fetchone()["id"]
        result.batch_id = batch_id
        occurrences: dict[str, int] = {}
        ctx = categorize.load_context(conn)          # rules, accounts and the look-alike index, loaded once for every row
        for row in rows:
            payee, upi_ref = narration_details(row.narration)
            if row.ref and re.fullmatch(r"\d{12}", row.ref):
                upi_ref = upi_ref or row.ref
            first_print = _fingerprint(account_id, row)
            occurrences[first_print] = occurrences.get(first_print, 0) + 1
            fingerprint = _fingerprint(account_id, row, occurrences[first_print])

            if upi_ref:
                existing = conn.execute(
                    "SELECT id, account_id FROM transactions WHERE upi_ref = %s AND direction = %s", (upi_ref, row.direction)
                ).fetchone()
                if existing:
                    if existing["account_id"] is None:
                        conn.execute("UPDATE transactions SET account_id = %s WHERE id = %s", (account_id, existing["id"]))
                    result.duplicates += 1
                    continue
            if conn.execute("SELECT 1 FROM transactions WHERE fingerprint = %s", (fingerprint,)).fetchone():
                result.duplicates += 1
                continue

            match = conn.execute(
                """SELECT id FROM transactions
                   WHERE amount = %s AND direction = %s
                     AND txn_date BETWEEN %s AND %s
                     AND upi_ref IS NULL AND fingerprint IS NULL
                     AND source IN ('chat', 'manual', 'sms')
                     AND (account_id IS NULL OR account_id = %s)
                   ORDER BY abs(txn_date - %s::date), id LIMIT 1""",
                (row.amount, row.direction, row.txn_date - timedelta(days=2), row.txn_date + timedelta(days=2),
                 account_id, row.txn_date),
            ).fetchone()
            if match:
                conn.execute(
                    """UPDATE transactions SET account_id = %s, upi_ref = %s, fingerprint = %s,
                              description = COALESCE(description, %s), txn_date = %s
                       WHERE id = %s""",
                    (account_id, upi_ref, fingerprint, row.narration, row.txn_date, match["id"]),
                )
                result.merged += 1
                continue

            category_id, why = _categorize_detail(conn, payee, row.narration, row.direction, ctx)
            suggested_id = _suggestion_id(conn, why)
            # Rows the fixed layers could not place stay in the catch-all and wait ('pending') for the optional model pass.
            conn.execute(
                """INSERT INTO transactions (account_id, amount, direction, txn_date, payee, description,
                                             upi_ref, fingerprint, category_id, source, raw_text, import_batch_id,
                                             category_source, category_confidence, category_reason,
                                             suggested_category_id, suggested_confidence, classify_state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'statement', %s, %s, %s, %s, %s, %s, %s, %s)""",
                (account_id, row.amount, row.direction, row.txn_date, payee, row.narration,
                 upi_ref, fingerprint, category_id, f"line {row.line}: {row.narration}", batch_id,
                 why.source, why.confidence, why.reason, suggested_id, why.suggested_confidence if suggested_id else None,
                 "pending" if why.source == "fallback" else None),
            )
            result.added += 1

        with_balance = [r for r in rows if r.balance is not None]
        if with_balance:
            first, last = with_balance[0], with_balance[-1]
            opening_balance = first.balance + (first.amount if first.direction == "debit" else -first.amount)
            opening_date = first.txn_date - timedelta(days=1)
            closing_date = last.txn_date
            changes = []
            for as_of, balance in ((opening_date, opening_balance), (closing_date, last.balance)):
                before = conn.execute(
                    "SELECT balance, source FROM balance_snapshots WHERE account_id = %s AND as_of = %s", (account_id, as_of)
                ).fetchone()
                conn.execute(
                    """INSERT INTO balance_snapshots (account_id, as_of, balance, source) VALUES (%s, %s, %s, 'statement')
                       ON CONFLICT (user_id, account_id, as_of) DO UPDATE SET balance = EXCLUDED.balance, source = 'statement'""",
                    (account_id, as_of, balance),
                )
                changes.append({"as_of": as_of.isoformat(), "new_balance": str(balance),
                                "prev_balance": str(before["balance"]) if before else None,
                                "prev_source": before["source"] if before else None})
            snapshot_changes = json.dumps(changes)
            result.opening = (opening_date, opening_balance)
            result.closing = (closing_date, last.balance)
        else:
            snapshot_changes = "[]"
        conn.execute(
            """UPDATE import_batches SET added = %s, merged = %s, duplicates = %s, needs_review = %s,
                      snapshot_changes = %s::jsonb WHERE id = %s""",
            (result.added, result.merged, result.duplicates, len(result.needs_review), snapshot_changes, batch_id),
        )
    return result


def import_file(data: bytes, filename: str, account: str | int, mapping: dict | None = None,
                password: str | None = None) -> ImportResult:
    table = read_table(data, filename, password)
    rows, problems = normalise(table, mapping or detect_columns(table))
    result = import_rows(rows, account, filename)
    result.skipped = problems
    return result
