"""Bank statement PDFs -> raw text and tables. Deterministic: pdfplumber reads the PDF's own text layer.

No OCR, and no AI model ever sees the PDF. If a PDF has no text layer (a scan or a photo) Rectifi says so and
asks for a CSV/XLSX instead of guessing.

Two readers feed the normal importer (`importer.read_table` does the gluing):
  1. ruled tables  -> every table on every page, header repeated per page, wrapped cells
  2. a text-line parser for statements with no ruling: a line starts with a date, the trailing numbers are the
     amount (and the running balance). Money in vs money out comes from how the balance moved, else from a Dr/Cr
     mark on the amount. If neither decides it, the row is marked "needs review" and is NOT imported by guess.

The password is only ever passed to pdfplumber. It is never stored, logged or put in a message.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from .money import fmt_inr, parse_amount
from .tools import RectifiError

MAX_PAGES = 300

SCAN_MESSAGE = (
    "This PDF has no text in it (it looks like a scan or a photo), so Rectifi can't read it. "
    "Download the CSV or Excel statement from net banking instead, or a PDF that you can select text in."
)
NO_ROWS_MESSAGE = (
    "I couldn't find any transactions in this PDF. Rectifi reads tables, and lines that start with a date and end "
    "with an amount and a balance. Try the CSV or Excel statement from net banking."
)
NEEDS_PASSWORD_MESSAGE = (
    "This PDF is password-protected. If your bank's email gave a password for this PDF, enter it and try again."
)
WRONG_PASSWORD_MESSAGE = "That password did not open the PDF. Check it for typing mistakes and try again."
UNREADABLE_MESSAGE = (
    "Rectifi couldn't read this file as a PDF. It may be damaged, or only partly downloaded. "
    "Download the statement again, or use the CSV or Excel version."
)


@dataclass
class PdfPage:
    tables: list[list[list[str]]] = field(default_factory=list)   # cells already cleaned: str, "" when empty
    lines: list[str] = field(default_factory=list)


@dataclass
class PdfContent:
    pages: list[PdfPage]


# ───────────────────────── reading the file ─────────────────────────

_JOIN_PUNCT = "-/@._"


def join_wrapped(first: str, second: str) -> str:
    """Join two pieces of one narration that the PDF broke across lines.

    A long reference gets cut anywhere, even in the middle of a number. A split number must be glued back
    (otherwise a 12-digit UPI reference stops being one), but two words must keep their space. Rules:
    glue when either side touches punctuation like '-' or '@', or when a digit meets a digit. Otherwise use a space.
    """
    first, second = first.strip(), second.strip()
    if not first or not second:
        return first or second
    if first[-1] in _JOIN_PUNCT or second[0] in _JOIN_PUNCT or (first[-1].isdigit() and second[0].isdigit()):
        return first + second
    return first + " " + second


def _clean_cell(value) -> str:
    if value is None:
        return ""
    pieces = [p.strip() for p in str(value).split("\n") if p.strip()]
    text = ""
    for piece in pieces:
        text = join_wrapped(text, piece) if text else piece
    return re.sub(r"[ \t]+", " ", text).strip()


def read_pdf(data: bytes, password: str | None = None) -> PdfContent:
    """Open the PDF and return every page's tables and text lines. Raises RectifiError with a plain message."""
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect

    try:
        pdf = pdfplumber.open(io.BytesIO(data), password=password or None)
    except Exception as exc:                          # pdfplumber wraps parser errors in PdfminerException
        inner = exc.args[0] if exc.args and isinstance(exc.args[0], BaseException) else exc
        if isinstance(inner, PDFPasswordIncorrect) or type(inner).__name__ in ("PDFPasswordIncorrect", "PDFEncryptionError"):
            raise RectifiError(WRONG_PASSWORD_MESSAGE if password else NEEDS_PASSWORD_MESSAGE) from None
        raise RectifiError(UNREADABLE_MESSAGE) from None

    pages: list[PdfPage] = []
    characters = 0
    try:
        with pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise RectifiError(f"This PDF has {len(pdf.pages)} pages. Rectifi reads up to {MAX_PAGES}. "
                                   "Download a shorter period, or the CSV or Excel version.")
            for page in pdf.pages:
                characters += len(page.chars)
                try:
                    tables = [[[_clean_cell(cell) for cell in row] for row in table] for table in page.extract_tables()]
                except Exception:                    # one odd page must not lose the others
                    tables = []
                text = page.extract_text() or ""
                lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
                pages.append(PdfPage(tables=tables, lines=[line for line in lines if line]))
    except RectifiError:
        raise
    except Exception:
        raise RectifiError(UNREADABLE_MESSAGE) from None
    if characters == 0:
        raise RectifiError(SCAN_MESSAGE)
    return PdfContent(pages=pages)


# ───────────────────────── text-line parser ─────────────────────────

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
DATE_AT_START = re.compile(
    rf"(?i)^\s*(\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}[ -](?:{_MONTHS})[a-z]*[ ,-]*\d{{2,4}})(?=\s|$)"
)
TRAILING_DATE = re.compile(r"\s+\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}$")
NUMBER = re.compile(r"(?i)^(-?(?:\d{1,3}(?:,\d{2,3})+|\d+)\.\d{2})(dr|cr)?\.?$")
MARK_ONLY = re.compile(r"(?i)^(dr|cr)\.?$")
OPENING = re.compile(r"(?i)\b(?:opening balance|balance brought forward|brought forward|b/f)\b\D{0,20}?(-?\d[\d,]*\.\d{2})\s*(dr|cr)?")
BALANCE_ROW = re.compile(r"(?i)\b(opening balance|closing balance|brought forward|carried forward|b/f|c/f)\b")
NOISE = re.compile(
    r"(?i)(page\s+\d+|computer generated|statement|account no|opening balance|closing balance|\btotal\b|summary|"
    r"registered office|customer care|toll free|www\.|http|generated on|end of|date\s+narration|continued)"
)


@dataclass
class _Line:
    date: str
    narration: str
    amount: Decimal
    balance: Decimal | None
    mark: str | None          # "debit" / "credit" from a Dr/Cr mark on the amount, if the line had one
    direction: str | None = None
    review: str | None = None


def _as_decimal(token: str) -> Decimal | None:
    try:
        return parse_amount(token)
    except ValueError:
        return None


def _split_amounts(rest: str) -> tuple[str, list[tuple[Decimal, str | None]]]:
    """'UPI-X-PAYMENT 250.00 24,750.00' -> ('UPI-X-PAYMENT', [(250.00, None), (24750.00, None)]).
    Only numbers with exactly two decimals count, so reference numbers and dates are never mistaken for money."""
    tokens = rest.split()
    end = len(tokens)
    found: list[tuple[Decimal, str | None]] = []
    while end > 0 and len(found) < 4:
        token = tokens[end - 1]
        if MARK_ONLY.match(token) and end >= 2:
            match = NUMBER.match(tokens[end - 2])
            if match and not match.group(2) and (value := _as_decimal(match.group(1))) is not None:
                found.append((value, "debit" if token[:2].lower() == "dr" else "credit"))
                end -= 2
                continue
            break
        match = NUMBER.match(token)
        if not match or (value := _as_decimal(match.group(1))) is None:
            break
        mark = match.group(2)
        found.append((value, ("debit" if mark.lower() == "dr" else "credit") if mark else None))
        end -= 1
    found.reverse()
    return " ".join(tokens[:end]), found


def _line_from(date_text: str, rest: str) -> tuple[_Line | None, str | None]:
    """One text line that starts with a date -> (_Line, None), or (None, why-not)."""
    narration, numbers = _split_amounts(rest)
    narration = TRAILING_DATE.sub("", narration).strip()        # a value date printed before the amounts
    if not numbers:
        return None, "no amount at the end of the line"
    if len(numbers) == 1:
        (amount, mark), balance = numbers[0], None
    elif len(numbers) == 2:
        (amount, mark), (balance, _) = numbers
    elif len(numbers) == 3:
        # withdrawal, deposit, balance: the empty side is printed as 0.00
        (first, first_mark), (second, second_mark), (balance, _) = numbers
        nonzero = [(v, m) for v, m in ((first, first_mark), (second, second_mark)) if v != 0]
        if len(nonzero) != 1:
            return None, "more than one amount on the line"
        amount, mark = nonzero[0]
    else:
        return None, "too many numbers at the end of the line"
    amount = abs(amount)
    if amount == 0:
        return None, "the amount is zero"
    return _Line(date_text, narration, amount, balance, mark), None


def _movement(previous: Decimal, amount: Decimal, balance: Decimal) -> str | None:
    """Which way did the balance move? None when it moved by some other amount."""
    if previous + amount == balance:
        return "credit"
    if previous - amount == balance:
        return "debit"
    return None


def decide_directions(lines: list[_Line], opening: Decimal | None) -> list[_Line]:
    """Set .direction on every line from the arithmetic. Never guesses: no proof means .review is set.

    1. balance before + amount = balance after  -> money in;   balance before - amount = balance after -> money out
    2. otherwise a Dr/Cr mark on the amount
    3. otherwise the row needs review
    A newest-first statement is turned around first, so the balance chain runs oldest to newest.
    """
    forward = reverse = 0
    for a, b in zip(lines, lines[1:]):
        if a.balance is not None and b.balance is not None:
            forward += _movement(a.balance, b.amount, b.balance) is not None
            reverse += _movement(b.balance, a.amount, a.balance) is not None
    if reverse > forward:
        lines.reverse()
    previous = opening
    for line in lines:
        by_balance = _movement(previous, line.amount, line.balance) if previous is not None and line.balance is not None else None
        if by_balance and line.mark and by_balance != line.mark:
            line.review = "the Dr/Cr mark and the balance disagree about money in or out"
        elif by_balance or line.mark:
            line.direction = by_balance or line.mark
        elif previous is None and line.balance is not None:
            line.review = "there is no earlier balance to compare with, and no Dr/Cr mark"
        elif line.balance is None:
            line.review = "no balance on this row and no Dr/Cr mark, so money in or out can't be proven"
        else:
            line.review = "the balance did not move by this amount, so a row may be missing before it"
        previous = line.balance
    return lines


def _is_continuation(text: str) -> bool:
    if NOISE.search(text):
        return False
    _, numbers = _split_amounts(text)
    return not numbers


def parse_text_pages(pages: list[list[str]]) -> tuple[list[_Line], Decimal | None, list[str]]:
    """Text lines of every page -> transaction lines, the opening balance if one is printed, and notes about lines skipped."""
    lines: list[_Line] = []
    problems: list[str] = []
    opening: Decimal | None = None
    for page in pages:
        current: _Line | None = None          # a wrapped narration only continues on the same page
        wrapped = 0
        for text in page:
            match = DATE_AT_START.match(text)
            rest = text[match.end():].strip() if match else text
            # "Opening Balance 25,000.00" as its own line, or after a date. Never read it out of a narration.
            if opening is None and (not match or BALANCE_ROW.match(rest)) and (m := OPENING.search(rest)):
                value = _as_decimal(m.group(1))
                if value is not None:
                    opening = -abs(value) if (m.group(2) or "").lower() == "dr" else value
            if match:
                current, wrapped = None, 0
                if BALANCE_ROW.match(rest):
                    continue
                line, why = _line_from(match.group(1), rest)
                if line is None:
                    problems.append(f"Skipped a line that starts with {match.group(1)}: {why}.")
                    continue
                lines.append(line)
                current = line
                continue
            if current is not None and wrapped < 2 and _is_continuation(text):
                current.narration = join_wrapped(current.narration, text)
                wrapped += 1
            else:
                current = None
    return lines, opening, problems


def table_from_text(pages: list[list[str]]) -> tuple[pd.DataFrame, list[str]]:
    """The fallback reader's result, shaped like a bank's own table so the usual column detection works.
    Columns: date, narration, debit, credit, balance, plus two hidden ones (__review, __amount) for rows that
    need a human look."""
    lines, opening, problems = parse_text_pages(pages)
    lines = decide_directions(lines, opening)
    rows = []
    for line in lines:
        amount_text = f"{line.amount:.2f}"
        rows.append({
            "date": line.date,
            "narration": line.narration,
            "debit": amount_text if line.direction == "debit" else "",
            "credit": amount_text if line.direction == "credit" else "",
            "balance": f"{line.balance:.2f}" if line.balance is not None else "",
            "__review": line.review or "",
            "__amount": amount_text if line.review else "",
        })
    frame = pd.DataFrame(rows, columns=["date", "narration", "debit", "credit", "balance", "__review", "__amount"], dtype=object)
    return frame, problems


def describe_review(amount: Decimal, narration: str, reason: str) -> str:
    return f"{fmt_inr(amount)} {narration[:50]}: {reason}"

