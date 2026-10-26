"""Total quantity to supply, worked out from Dose x Frequency x Duration.

  per day  = the numbers in the frequency added up (1-0-1 -> 2, 1-1-1-1 -> 4) or the words ("Twice daily" -> 2)
  total    = per day x the amount in the Dose (1 tablet -> 1, 2 tablets -> 2, 5 mL -> 5) x number of days, rounded UP.

It is only a suggestion: the doctor can overwrite it. SOS / as-needed cannot be calculated.
Whole numbers are used (thousandths) so this gives the same answer as static/js/rx_calc.js.
"""
import re
from collections import namedtuple

CalcResult = namedtuple("CalcResult", "quantity explanation")
SCALE = 1000
_FRACTION = {"\u00bd": 500, "\u00bc": 250, "\u00be": 750}
_NUM = r"[0-9\u00bd\u00bc\u00be][0-9\u00bd\u00bc\u00be./]*"
_PATTERN = re.compile(rf"^\s*({_NUM}(?:\s*[-\u2013]\s*{_NUM}){{1,3}})\s*$")


def _scaled(token):
    """'1' -> 1000, '1/2' -> 500, '0.5' -> 500, the half sign -> 500. None if it is not a number."""
    t = token.strip()
    if t in _FRACTION:
        return _FRACTION[t]
    try:
        if "/" in t:
            top, bottom = t.split("/", 1)
            return int((float(top) * SCALE) / float(bottom) + 0.5)
        return int(float(t) * SCALE + 0.5)
    except (ValueError, ZeroDivisionError):
        return None


_WORDS = [
    (re.compile(r"^(once( a| per)? day|once daily|daily|od|qd)$"), 1000),
    (re.compile(r"^(twice( a| per)? day|twice daily|bd|bid)$"), 2000),
    (re.compile(r"^(three times( a| per)? day|three times daily|thrice( daily)?|tds|tid)$"), 3000),
    (re.compile(r"^(four times( a| per)? day|four times daily|qid|qds)$"), 4000),
    (re.compile(r"^(at bedtime|bedtime|hs|at night|night|nocte)$"), 1000),
]
_EVERY = re.compile(r"^every\s+(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)$")


def doses_per_day(frequency):
    """Doses per day x 1000, or None when it cannot be worked out (for example SOS)."""
    text = " ".join((frequency or "").lower().replace(".", "").split())
    if not text:
        return None
    m = _PATTERN.match(text)
    if m:
        parts = [_scaled(x) for x in re.split(r"[-\u2013]", m.group(1))]
        return None if None in parts else sum(parts)
    for rx, value in _WORDS:
        if rx.match(text):
            return value
    e = _EVERY.match(text)
    if e and float(e.group(1)) > 0:
        return int(24000 / float(e.group(1)) + 0.5)
    return None


_DOSE = re.compile(r"^\s*(?:(\d+)\s*([\u00bd\u00bc\u00be])|(\d+(?:\.\d+)?(?:/\d+)?|[\u00bd\u00bc\u00be]))")


def dose_amount(dose):
    """Amount per dose x 1000 from the start of the Dose text ('2 tablets' -> 2000). Blank or no number -> 1."""
    m = _DOSE.match(dose or "")
    if not m:
        return SCALE
    if m.group(1):
        return int(m.group(1)) * SCALE + _FRACTION[m.group(2)]
    return _scaled(m.group(3)) or SCALE


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*$")
_UNIT_DAYS = {"": 1, "d": 1, "day": 1, "days": 1, "w": 7, "wk": 7, "wks": 7, "week": 7, "weeks": 7,
              "mo": 30, "mos": 30, "month": 30, "months": 30}


def duration_days(duration):
    m = _DURATION.match((duration or "").lower())
    if not m or m.group(2) not in _UNIT_DAYS:
        return None
    days = int(float(m.group(1)) * _UNIT_DAYS[m.group(2)] + 0.5)
    return days or None


def _g(scaled):
    return f"{scaled / SCALE:g}"


def calc_quantity(dose, frequency, duration):
    per_day, days = doses_per_day(frequency), duration_days(duration)
    if per_day is None:
        return CalcResult(None, "Enter the quantity yourself: this frequency cannot be counted automatically.")
    if days is None:
        return CalcResult(None, "Enter the duration (for example 5 days) to calculate the quantity.")
    amount = dose_amount(dose)
    total = -(-(per_day * amount * days) // (SCALE * SCALE))  # round up
    return CalcResult(total, f"{_g(per_day)} dose(s) a day \u00d7 {_g(amount)} per dose \u00d7 {days} day(s) = {total}")
