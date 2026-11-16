"""Deterministic checks for model output. The model proposes {value, evidence}; this code decides.

A field survives only if (1) its quote appears in the source, (2) the value follows from the quote,
(3) the normalization (amount, currency, stage, name) is valid. Otherwise it becomes null."""
import re
import unicodedata
from decimal import Decimal

from app.services.companies import canonical

UNITS = {"k": 10**3, "thousand": 10**3, "m": 10**6, "mn": 10**6, "mm": 10**6, "million": 10**6, "b": 10**9, "bn": 10**9,
         "billion": 10**9, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "cr": 10**7, "crore": 10**7, "crores": 10**7}
SYMBOLS = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
           "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR"}
WORD_CCY = {"dollars": "USD", "dollar": "USD", "euros": "EUR", "euro": "EUR", "pounds": "GBP", "rupees": "INR"}
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_UNIT = r"(" + "|".join(sorted(UNITS, key=len, reverse=True)) + r")\b"
PREFIX_RE = re.compile(r"(us\$|\$|€|£|₹|rs\.?|inr|usd|eur|gbp)\s*" + _NUM + r"\s*" + "(?:" + _UNIT + ")?", re.I)
SUFFIX_RE = re.compile(_NUM + r"\s*" + _UNIT + r"\s*(dollars?|euros?|pounds|rupees|usd|eur|gbp|inr)", re.I)
STAGES = {
    "pre-seed": ["pre-seed", "pre seed", "preseed"], "seed": ["seed round", "seed funding", "seed"],
    "pre-series a": ["pre-series a", "pre series a"], "series a": ["series a"], "series b": ["series b"],
    "series c": ["series c"], "bridge": ["bridge round", "bridge"], "strategic": ["strategic round", "strategic investment"],
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"}))
    return " ".join(s.split())


def parse_amounts(text: str) -> list[tuple[Decimal, str | None]]:
    """Every (value, currency) mentioned: '$5M', '₹40 Cr', '5 million dollars', 'Rs 10 lakh', '€2.5m'."""
    out = []
    for m in PREFIX_RE.finditer(text):
        v = Decimal(m.group(2).replace(",", "")) * (UNITS[m.group(3).lower()] if m.group(3) else 1)
        out.append((v, SYMBOLS[m.group(1).lower()]))
    for m in SUFFIX_RE.finditer(text):
        ccy = m.group(3).lower()
        out.append((Decimal(m.group(1).replace(",", "")) * UNITS[m.group(2).lower()], WORD_CCY.get(ccy) or SYMBOLS.get(ccy)))
    return out


def parse_stage(text: str) -> str | None:
    t = norm(text)
    for canon, aliases in STAGES.items():
        if any(re.search(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])", t) for a in aliases):
            return canon
    return None


ABSENT = {"", "null", "none", "n/a", "na", "unknown", "not stated", "not mentioned"}


def _num(v) -> Decimal | None:
    """Tolerates formatting noise the model adds around a number ('{6000000}', '6,000,000'), never a different number."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v))
    return Decimal(m.group(0).replace(",", "")) if m else None


def validate_field(kind: str, value, quote: str | None, source: str, currency: str | None = None) -> tuple[object | None, str]:
    """Returns (clean value or None, status). status: supported | normalized | rejected:<reason>."""
    if value in (None, "", []) or str(value).strip().lower() in ABSENT or str(quote or "").strip().lower() in ABSENT:
        return None, "absent"
    if not quote or norm(quote) not in norm(source):
        return None, "rejected:quote_not_in_source"
    if kind in ("company", "person"):
        v = str(value)
        if norm(v) in norm(quote):
            return v, "supported"
        c = canonical(v)
        return (v, "normalized") if c and c in canonical(quote) else (None, "rejected:value_not_in_quote")
    if kind == "stage":
        s = parse_stage(str(value)) or norm(str(value))
        return (s, "supported" if norm(str(value)) == s else "normalized") if parse_stage(quote) == s else (None, "rejected:stage_not_in_quote")
    if kind == "amount":
        n = _num(value)
        if n is None:
            return None, "rejected:not_a_number"
        for v, ccy in parse_amounts(quote):
            if abs(v - n) <= n * Decimal("0.005"):
                if currency and ccy and currency.upper() != ccy:
                    return None, "rejected:currency_mismatch"
                return {"value": v, "currency": ccy or (currency or None)}, "normalized"
        return None, "rejected:amount_not_in_quote"
    raise ValueError(kind)


def ground(out: dict, source: str) -> dict:
    """Model output with {value, evidence} fields -> flat grounded dict plus per-field status and quotes."""
    res: dict = {"label": out["label"], "company": None, "stage": None, "amount_value": None, "currency": None,
                 "investors": [], "founders": [], "quotes": {}, "field_status": {}}
    fs, q = res["field_status"], res["quotes"]

    def one(key, kind, **kw):
        f = out.get(key)
        if not f:
            return None
        v, st = validate_field(kind, f.get("value"), f.get("evidence"), source, **kw)
        fs[key] = st
        if v is not None:
            q[key] = f["evidence"]
        return v

    res["company"] = one("company", "company")
    res["stage"] = one("stage", "stage")
    a = out.get("amount")
    amt = one("amount", "amount", currency=(a or {}).get("currency")) if a else None
    if amt:
        res["amount_value"], res["currency"] = str(amt["value"]), amt["currency"]
    for key in ("investors", "founders"):
        for f in out.get(key) or []:
            v, st = validate_field("person", f.get("value"), f.get("evidence"), source)
            fs.setdefault(key, []).append(st)
            if v:
                res[key].append(v)
                q[f"{key}:{v}"] = f["evidence"]
    return res
