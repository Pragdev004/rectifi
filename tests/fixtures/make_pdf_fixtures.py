"""Makes the fake bank-statement PDFs the tests use. Run it once; the PDFs are committed.

    pip install reportlab pypdf pillow      (development only: NOT in requirements.txt, the app never needs them)
    python tests/fixtures/make_pdf_fixtures.py

Every name, number and amount here is invented. Nothing comes from a real bank.

Files it writes (next to this script):
  statement_ruled.pdf      ruled table, 2 pages, header repeated on page 2, one narration wrapped inside its cell,
                           one wrapped onto its own row, blank separator rows, a small summary table at the end
  statement_text_only.pdf  no ruling at all: plain text lines, 2 pages, a wrapped narration, page footers
  statement_encrypted.pdf  a small ruled statement, password "demo1234"
  statement_image_only.pdf a picture of a statement: no text layer at all
"""

from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).resolve().parent
PASSWORD = "demo1234"
OPENING = "25000.00"

# (date, narration, ref, withdrawal, deposit)  -- in date order; balances are worked out below.
ROWS = [
    ("01/09/2026", "UPI-SWIGGY-swiggy@icici-ICIC0000103-429183746521-PAYMENT", "0000429183746521", "250.00", ""),
    ("02/09/2026", "NEFT CR-SBIN0001234-IITM ALLOWANCE-STIPEND SEP", "N234567890", "", "8000.00"),
    ("03/09/2026", "UPI-UBER INDIA-uber@axisbank-UTIB0000100-429900112233-RIDE", "0000429900112233", "1240.50", ""),
    ("04/09/2026", "POS 416021XXXXXX4417 DMART AVENUE CHENNAI SOUTH BRANCH TAMIL NADU IN", "POS0904", "2105.00", ""),
    ("05/09/2026", "UPI-NETFLIX-netflix@hdfcbank-HDFC0000001-430012345678-SUBSCRIPTION", "0000430012345678", "199.00", ""),
    ("07/09/2026", "UPI-ZOMATO-zomato@icici-ICIC0000103-430055566677-PAYMENT", "0000430055566677", "412.75", ""),
    ("08/09/2026", "UPI-RAPIDO-rapido@ybl-YESB0YBLUPI-430066677788-RIDE", "0000430066677788", "88.00", ""),
    ("09/09/2026", "ATM WDL KOTTAYAM VALAVOOR", "ATM0909", "2000.00", ""),
    ("10/09/2026", "UPI-REFUND AMAZON-amazon@apl-UTIB0000100-430077788899-REFUND", "0000430077788899", "", "649.00"),
    ("11/09/2026", "UPI-BIGBASKET-bigbasket@icici-ICIC0000103-430088899900-GROCERY", "0000430088899900", "1530.25", ""),
    ("12/09/2026", "UPI-SPOTIFY-spotify@axisbank-UTIB0000100-430099900011-SUBSCRIPTION", "0000430099900011", "119.00", ""),
    ("14/09/2026", "UPI-FRIEND RAVI-ravi@okaxis-UTIB0000100-430100011122-DINNER SPLIT", "0000430100011122", "", "350.00"),
    ("15/09/2026", "UPI-BOOKMYSHOW-bms@icici-ICIC0000103-430111122233-MOVIE", "0000430111122233", "600.00", ""),
    ("16/09/2026", "UPI-APOLLO PHARMACY-apollo@ybl-YESB0YBLUPI-430122233344-MEDICINE", "0000430122233344", "318.40", ""),
    ("17/09/2026", "UPI-IRCTC-irctc@sbi-SBIN0000001-430133344455-TRAIN TICKET", "0000430133344455", "745.00", ""),
    ("18/09/2026", "UPI-SWIGGY-swiggy@icici-ICIC0000103-430144455566-PAYMENT", "0000430144455566", "250.00", ""),
    ("19/09/2026", "UPI-SWIGGY-swiggy@icici-ICIC0000103-430155566677-PAYMENT", "0000430155566677", "250.00", ""),
    ("21/09/2026", "NEFT CR-SBIN0001234-HOME TRANSFER", "N234567891", "", "5000.00"),
    ("22/09/2026", "UPI-MESS FEES-messfees@sbi-SBIN0000001-430166677788-HOSTEL MESS", "0000430166677788", "3500.00", ""),
    ("23/09/2026", "UPI-CHAI POINT-chaipoint@ybl-YESB0YBLUPI-430177788899-TEA", "0000430177788899", "40.00", ""),
    ("24/09/2026", "UPI-COURSERA-coursera@icici-ICIC0000103-430188899900-COURSE", "0000430188899900", "2999.00", ""),
    ("26/09/2026", "UPI-AIRTEL-airtel@icici-ICIC0000103-430199900011-RECHARGE", "0000430199900011", "299.00", ""),
    ("28/09/2026", "UPI-DOMINOS-dominos@icici-ICIC0000103-430200011122-PIZZA", "0000430200011122", "389.00", ""),
    ("30/09/2026", "INTEREST CREDIT", "INT0930", "", "112.35"),
]
# a few narrations that the bank prints on a second line
WRAPPED_ON_NEXT_ROW = {"ATM WDL KOTTAYAM VALAVOOR": "CARD XX4417 TXN ID 99812"}


def money(value) -> str:
    """Decimal-free formatting is fine here: this script only makes text, it does no arithmetic that is checked."""
    from decimal import Decimal

    return f"{Decimal(value):,.2f}"


def with_balances(rows):
    from decimal import Decimal

    balance = Decimal(OPENING)
    out = []
    for date, narration, ref, debit, credit in rows:
        balance += Decimal(credit or "0") - Decimal(debit or "0")
        out.append((date, narration, ref, debit, credit, f"{balance:.2f}"))
    return out


# ───────────────────────── ruled-table PDF ─────────────────────────

CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=7, leading=8.6)
HEAD = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]


def ruled_table(rows, with_blank_rows=True):
    data = [HEAD]
    for index, (date, narration, ref, debit, credit, balance) in enumerate(rows):
        extra = WRAPPED_ON_NEXT_ROW.get(narration)
        data.append([date, Paragraph(narration, CELL), ref, date, money(debit) if debit else "",
                     money(credit) if credit else "", money(balance)])
        if extra:                                   # wrapped onto its own row: only the narration cell is filled
            data.append(["", Paragraph(extra, CELL), "", "", "", "", ""])
        if with_blank_rows and index in (6, 15):    # a blank separator row, as some banks print
            data.append([""] * 7)
    table = Table(data, colWidths=[52, 190, 78, 52, 60, 56, 62], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONT", (0, 0), (-1, -1), "Helvetica", 7),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
    ]))
    return table


def build_ruled(path: Path, rows, small_page: bool, password: str | None = None) -> None:
    buffer = io.BytesIO()
    page = (595, 440) if small_page else A4
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=20, rightMargin=20, topMargin=24, bottomMargin=24)
    title = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=10)
    small = ParagraphStyle("s", fontName="Helvetica", fontSize=7)
    story = [Paragraph("DEMO BANK LTD. Statement of Account", title),
             Paragraph("Account No: XXXXXXXX4417 | Period: 01/09/2026 to 30/09/2026", small), Spacer(1, 6),
             ruled_table(rows), Spacer(1, 10), Paragraph("Statement summary", title)]
    from decimal import Decimal

    total_debits = sum(Decimal(r[3] or "0") for r in rows)
    total_credits = sum(Decimal(r[4] or "0") for r in rows)
    summary = Table([["Opening Balance", "Total Debits", "Total Credits", "Closing Balance"],
                     [money(OPENING), money(total_debits), money(total_credits), money(rows[-1][5])]],
                    colWidths=[100, 100, 100, 100])
    summary.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONT", (0, 0), (-1, -1), "Helvetica", 7)]))
    story.append(summary)
    doc.build(story)
    write(path, buffer.getvalue(), password)


# ───────────────────────── text-only PDF ─────────────────────────

def build_text_only(path: Path, rows) -> None:
    """No lines, no boxes: only text placed in columns, like a statement printed from a plain report."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 470))
    per_page = 12
    lines = []
    for date, narration, ref, debit, credit, balance in rows:
        amount = debit or credit
        extra = WRAPPED_ON_NEXT_ROW.get(narration)
        lines.append((date, narration, money(amount), money(balance), extra))
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    for number, chunk in enumerate(pages, start=1):
        y = 440
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(30, y, "DEMO BANK LTD   Statement of Account")
        y -= 12
        pdf.setFont("Helvetica", 7)
        pdf.drawString(30, y, "Account No: XXXXXXXX4417    Period: 01/09/2026 to 30/09/2026")
        y -= 14
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(30, y, "Date")
        pdf.drawString(95, y, "Narration")
        pdf.drawString(420, y, "Amount")
        pdf.drawString(500, y, "Balance")
        y -= 12
        pdf.setFont("Helvetica", 7)
        if number == 1:
            pdf.drawString(95, y, f"Opening Balance {money(OPENING)}")
            y -= 12
        for date, narration, amount, balance, extra in chunk:
            pdf.drawString(30, y, date)
            pdf.drawString(95, y, narration[:60])
            pdf.drawRightString(470, y, amount)
            pdf.drawRightString(550, y, balance)
            y -= 11
            if len(narration) > 60:
                pdf.drawString(95, y, narration[60:])
                y -= 11
            if extra:
                pdf.drawString(95, y, extra)
                y -= 11
        pdf.setFont("Helvetica", 6)
        pdf.drawString(30, 20, f"Page {number} of {len(pages)}")
        pdf.drawString(200, 20, "This is a computer generated statement and needs no signature.")
        pdf.showPage()
    pdf.save()
    write(path, buffer.getvalue(), None)


# ───────────────────────── image-only PDF ─────────────────────────

def build_image_only(path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "DEMO BANK  Statement of Account (a photo, not text)", fill="black")
    for i in range(6):
        draw.text((20, 60 + i * 30), f"0{i + 1}/09/2026   UPI-SOMETHING   {100 + i}.00   {9000 - i * 100}.00", fill="black")
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(600, 300))
    from reportlab.lib.utils import ImageReader

    pdf.drawImage(ImageReader(image), 0, 0, width=600, height=300)
    pdf.showPage()
    pdf.save()
    write(path, buffer.getvalue(), None)


# ───────────────────────── writing (and optional encryption) ─────────────────────────

def write(path: Path, data: bytes, password: str | None) -> None:
    if password:
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        writer.encrypt(user_password=password, owner_password=password + "-owner", algorithm="AES-128")
        buffer = io.BytesIO()
        writer.write(buffer)
        data = buffer.getvalue()
    path.write_bytes(data)
    print(f"wrote {path.name} ({len(data)} bytes)")


def main() -> None:
    rows = with_balances(ROWS)
    build_ruled(HERE / "statement_ruled.pdf", rows, small_page=True)
    build_text_only(HERE / "statement_text_only.pdf", rows)
    build_ruled(HERE / "statement_encrypted.pdf", with_balances(ROWS[:6]), small_page=False, password=PASSWORD)
    build_image_only(HERE / "statement_image_only.pdf")
    from decimal import Decimal

    debits = sum(Decimal(r[3] or "0") for r in rows)
    credits = sum(Decimal(r[4] or "0") for r in rows)
    print(f"rows={len(rows)} debits={debits} credits={credits} opening={OPENING} closing={rows[-1][5]}")


if __name__ == "__main__":
    main()
