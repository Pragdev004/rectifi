"""Deterministic parsers for the text banks send you: transaction SMS and statement narrations.

Regex, not an LLM: amounts extracted here are exact or the parse fails loudly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .money import parse_amount

AMOUNT = re.compile(r"(?i)(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d{1,2})?)")
DEBIT_WORDS = re.compile(r"(?i)\b(debited|debit|spent|paid|sent|withdrawn|purchase|dr\b|transferred to|trf to)")
CREDIT_WORDS = re.compile(r"(?i)\b(credited|credit|received|deposited|refund(?:ed)?|cr\b)")
ACCOUNT = re.compile(r"(?i)(?:a/c|acct|account|card|ac)\s*(?:no\.?)?\s*(?:ending\s*(?:with)?\s*)?[x*]*\s*(\d{3,6})")
REF = re.compile(r"(?i)(?:upi\s*ref(?:\s*no)?|ref(?:erence)?\s*(?:no|number)?|rrn|utr|txn\s*id)[\s.:#-]*(\d{9,16})")
# No hyphen in the local part: statement narrations use '-' as a field separator ("UPI-SWIGGY-swiggy@icici-…").
VPA = re.compile(r"(?i)\b([a-z0-9._]{2,}@[a-z][a-z0-9]{1,})\b")
TO_PAYEE = re.compile(r"(?i)\b(?:to|at|towards|trf to|paid to|sent to)\s+(?:vpa\s+)?([A-Za-z0-9&.' -]{2,40}?)(?=\s+(?:on|ref|upi|via|from|avl|avbl|bal|if|not)\b|[.,;]|$)")
FROM_PAYEE = re.compile(r"(?i)\b(?:from|by)\s+(?:vpa\s+)?([A-Za-z0-9&.' -]{2,40}?)(?=\s+(?:on|ref|upi|via|to|avl|avbl|bal|if)\b|[.,;]|$)")
DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{4})\b"), ("%d-%m-%Y", "%d/%m/%Y")),
    (re.compile(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2})\b"), ("%d-%m-%y", "%d/%m/%y")),
    (re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"), ("%Y-%m-%d",)),
    (re.compile(r"(?i)\b(\d{1,2}[- ]?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[- ]?\d{2,4})\b"),
     ("%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d %b %y", "%d%b%y", "%d%b%Y", "%d-%B-%Y", "%d %B %Y")),
]
NOISE = re.compile(r"(?i)\b(your|a/c|acct|account|card|bank|upi|imps|neft|rtgs)\b")


@dataclass
class ParsedSms:
    amount: Decimal
    direction: str
    txn_date: date | None = None
    account_last4: str | None = None
    payee: str | None = None
    upi_ref: str | None = None
    warnings: list[str] = field(default_factory=list)


def _parse_date(text: str) -> date | None:
    for pattern, formats in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(1)
        for fmt in formats:
            for candidate in (raw, raw.replace("/", "-")):
                try:
                    return datetime.strptime(candidate, fmt).date()
                except ValueError:
                    continue
    return None


def _clean_payee(value: str | None) -> str | None:
    if not value:
        return None
    value = NOISE.sub("", value).strip(" .-:")
    return value or None


def parse_sms(text: str) -> ParsedSms | None:
    """Parse a bank transaction SMS. Returns None if it doesn't look like one (OTPs, promos, balance alerts)."""
    if not text or re.search(r"(?i)\b(otp|one time password)\b", text):
        return None
    amount_match = AMOUNT.search(text)
    if not amount_match:
        return None
    debit = DEBIT_WORDS.search(text)
    credit = CREDIT_WORDS.search(text)
    if debit and credit:
        # "debited ... credited to X" — whichever word comes first describes YOUR account
        direction = "debit" if debit.start() < credit.start() else "credit"
    elif debit:
        direction = "debit"
    elif credit:
        direction = "credit"
    else:
        return None
    parsed = ParsedSms(amount=parse_amount(amount_match.group(1)), direction=direction)
    parsed.txn_date = _parse_date(text)
    if parsed.txn_date is None:
        parsed.warnings.append("No date found in the SMS — using today.")
    if (m := ACCOUNT.search(text)):
        parsed.account_last4 = m.group(1)[-4:]
    if (m := REF.search(text)):
        parsed.upi_ref = m.group(1)
    if (m := VPA.search(text)):
        parsed.payee = m.group(1).lower()
    else:
        m = (TO_PAYEE if direction == "debit" else FROM_PAYEE).search(text)
        parsed.payee = _clean_payee(m.group(1)) if m else None
    return parsed


# ─────────────── statement narrations ───────────────

UPI_REF_IN_NARRATION = re.compile(r"(?<!\d)(\d{12})(?!\d)")


def narration_details(narration: str) -> tuple[str | None, str | None]:
    """'UPI/DR/429183746521/SWIGGY/YESB/swiggy@ybl/Payment' -> (payee, upi_ref).

    Handles the common SBI / HDFC / ICICI / Axis shapes: a 12-digit reference somewhere,
    and the merchant either as a VPA or as the first word-ish segment that isn't a code.
    """
    if not narration:
        return None, None
    text = str(narration)
    upi_ref = None
    if re.search(r"(?i)\bupi\b", text):
        m = UPI_REF_IN_NARRATION.search(text)
        upi_ref = m.group(1) if m else None
    vpa = VPA.search(text)
    if vpa:
        return vpa.group(1).lower(), upi_ref
    segments = [s.strip() for s in re.split(r"[/\-|]", text) if s.strip()]
    skip = re.compile(r"(?i)^(upi|dr|cr|p2a|p2m|imps|neft|rtgs|mb|ib|pos|atm|ach|nach|payment|pay|txn|ref|\d+|[a-z]{4}0[a-z0-9]{6}|[a-z]{2,4})$")
    for seg in segments:
        if not skip.match(seg) and re.search(r"[A-Za-z]{3,}", seg):
            return seg.strip()[:60], upi_ref
    return None, upi_ref


# ─────────────── phone SMS: what is worth keeping? ───────────────
# Used by the phone gateway (rectifi/sms_gateway.py -> tools.ingest_sms). All fixed rules, no model.

MONEY_WORD = re.compile(r"(?i)(?:rs\.?|inr|₹)\s*\d")
TXN_VERB = re.compile(r"(?i)\b(debited|credited|spent|received|withdrawn|sent|paid|transferred|deposited|refunded)\b")
OTP_WORD = re.compile(r"(?i)\b(otp|one[- ]time password)\b")
PROMO_WORD = re.compile(
    r"(?i)\b(offers?|pre-?approved|apply now|congratulations|click|reward points?|t&c|tnc|download)\b|https?://|bit\.ly|www\."
)
PHONE_NUMBER_SENDER = re.compile(r"^\+?\d{10,}$")


@dataclass
class SmsVerdict:
    """What to do with one forwarded SMS. kind is 'transaction' (add it), 'review' (a person should look) or 'ignored'."""
    kind: str
    parsed: ParsedSms | None = None
    reason: str | None = None


def classify_sms(text: str, sender: str | None = None) -> SmsVerdict:
    """OTPs, promos and chats are ignored. A clear debit/credit SMS is a transaction. A bank-looking SMS the fixed
    rules cannot be sure about goes to 'review' instead of being guessed.

    Safety rules for automatic capture (stricter than the Paste SMS box, which has a person watching):
      - a sender that is a plain phone number (a friend's chat) is ignored
      - an SMS mentioning an OTP is never added; if it also says debited/credited it goes to review
      - the SMS must contain a real past-tense word (debited, credited, spent, received, withdrawn, sent, ...),
        so "credit limit of Rs 2,00,000" is not income
      - offer/promo wording keeps it out of automatic adding
    """
    text = (text or "").strip()
    if not text:
        return SmsVerdict("ignored", reason="empty")
    if sender and PHONE_NUMBER_SENDER.match(re.sub(r"[\s-]", "", sender)):
        return SmsVerdict("ignored", reason="sent from a personal phone number, not a bank sender id")
    has_money = bool(MONEY_WORD.search(text))
    has_verb = bool(TXN_VERB.search(text))
    promo = bool(PROMO_WORD.search(text))
    if OTP_WORD.search(text):
        if has_money and has_verb:
            return SmsVerdict("review", reason="mentions an OTP and also says money moved, so it was not added automatically")
        return SmsVerdict("ignored", reason="OTP")
    parsed = parse_sms(text)
    if parsed is None:
        if has_money and has_verb and not promo:
            return SmsVerdict("review", reason="looks like a bank SMS, but the amount or the direction could not be read")
        return SmsVerdict("ignored", reason="not a transaction")
    if has_verb and not promo:
        return SmsVerdict("transaction", parsed)
    if promo and not has_verb:
        return SmsVerdict("ignored", reason="looks like an offer, not a transaction")
    if has_verb:
        return SmsVerdict("review", parsed, "has offer or link wording, so it was not added automatically")
    return SmsVerdict("review", parsed, "no clear debited/credited word, so it was not added automatically")


def best_effort_sms(text: str) -> dict:
    """Whatever the fixed rules can pull out of an SMS, to pre-fill a form. Every field may be None; a person confirms it."""
    out: dict = {"amount": None, "direction": None, "txn_date": None, "payee": None, "account_last4": None, "upi_ref": None}
    if not text:
        return out
    if (m := AMOUNT.search(text)):
        try:
            out["amount"] = parse_amount(m.group(1))
        except ValueError:
            pass
    debit, credit = DEBIT_WORDS.search(text), CREDIT_WORDS.search(text)
    if debit and credit:
        out["direction"] = "debit" if debit.start() < credit.start() else "credit"
    elif debit or credit:
        out["direction"] = "debit" if debit else "credit"
    out["txn_date"] = _parse_date(text)
    if (m := ACCOUNT.search(text)):
        out["account_last4"] = m.group(1)[-4:]
    if (m := REF.search(text)):
        out["upi_ref"] = m.group(1)
    if (m := VPA.search(text)):
        out["payee"] = m.group(1).lower()
    elif out["direction"]:
        m = (TO_PAYEE if out["direction"] == "debit" else FROM_PAYEE).search(text)
        out["payee"] = _clean_payee(m.group(1)) if m else None
    return out
