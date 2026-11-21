"""Years of experience a job description asks for (J5b), rules only, no LLM. Ported in spirit from career-ops jd-years.mjs (MIT; read, English only, simpler).

Per sentence: find "N years", "N+", "N or more", "N-M", "at least N", "five (5)"; keep a mention only when it reads as the candidate's experience (not age,
tenure, vesting, a contract, a degree or the company's history); tag it required or preferred from the nearest cue or the section heading above it; and call
it overall ("5 years of experience") or about a skill ("3 years with SQL"). The figure is the LOWEST required overall floor, the most lenient honest reading,
so a posting is called above the band only when every bar it states is out of reach. Returns None when nothing clear is stated. Only the number is kept:
the description text is never stored (the board adapters keep title, location, team and link only)."""
import html
import re

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
NUM = r"(\d{1,2}|" + "|".join(WORDS) + r")"
MENTION = re.compile(rf"(?:(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than)\s+)?{NUM}(?:\s*\((\d{{1,2}})\))?(?:\s*(?:-|–|to|or)\s*{NUM})?\s*\+?\s*(?:or\s+more\s+)?(?:years?|yrs?)\b", re.IGNORECASE)
EXPERIENCE = re.compile(r"^\W*(?:of\s+)?(?:(?:relevant|professional|related|industry|work|working|hands-on|total|overall|proven|practical|full[- ]time|prior|software|engineering)\s+)*"
                        r"(?:experience|exp\b|background|working|work\b|in\b|as\b|building|managing|leading|developing|delivering|of\b)", re.IGNORECASE)
SKILL = re.compile(r"^\W*(?:of\s+)?(?:\w+\s+){0,4}?(?:experience|exp)\s+(?:with|using|on|of)\b|^\W*(?:with|using)\b", re.IGNORECASE)
NOT_EXPERIENCE = re.compile(r"\b(?:years?\s+old|age|aged|tenure|vest\w*|contract\s+(?:term|length)|term\s+of|founded|since\s+(?:19|20)\d\d|our\s+history|anniversary|"
                            r"4-year|four-year|degree\s+\(|warranty|lease|stock|bonus\s+year|years?\s+of\s+age)\b", re.IGNORECASE)
PREFERRED = re.compile(r"\b(?:preferred|nice\s+to\s+have|a\s+plus|bonus|ideal(?:ly)?|desirable|advantageous|would\s+be\s+(?:a\s+)?(?:great|an?\s+advantage))\b", re.IGNORECASE)
REQUIRED = re.compile(r"\b(?:required|requirements?|minimum|must|qualifications?|what\s+you(?:'ll|\s+will)\s+(?:bring|need)|you\s+have|you\s+bring)\b", re.IGNORECASE)
DEGREE_ALT = re.compile(r"\b(?:degree|bachelor|b\.?s\.?|master|m\.?s\.?|phd)\b.{0,60}\bor\b|\bor\b.{0,40}\b(?:equivalent|combination)\b", re.IGNORECASE)


def _n(s: str | None) -> int | None:
    return None if s is None else int(s) if s.isdigit() else WORDS.get(s.lower())


def years_required(text: str | None) -> int | None:
    """The lowest required overall experience floor stated in a description, in whole years, or None."""
    if not text:
        return None
    t = re.sub(r"(?i)<(?:br|/p|/li|/h\d|/ul)\s*/?>", "\n", html.unescape(text))
    t = re.sub(r"<[^>]+>", " ", html.unescape(t)).replace("\xa0", " ")
    section, floors = "required", []
    for line in re.split(r"\n+|(?<=[.;!?])\s+|\s[•·▪]\s", t):
        line = line.strip()
        if not line:
            continue
        if len(line) < 70 and (PREFERRED.search(line) or REQUIRED.search(line)) and not MENTION.search(line):
            section = "preferred" if PREFERRED.search(line) else "required"  # a heading line sets the section for what follows
            continue
        for m in MENTION.finditer(line):
            low = _n(m.group(1)) if m.group(2) is None else int(m.group(2))
            if low is None or not 0 < low <= 30 or NOT_EXPERIENCE.search(line):
                continue
            rest = line[m.end():]
            if not (EXPERIENCE.match(rest) or re.search(r"\bexperience\b", line[max(0, m.start() - 40):m.start()], re.IGNORECASE)):
                continue
            cue = PREFERRED.search(line)
            kind = "preferred" if cue else ("required" if REQUIRED.search(line) else section)
            if kind == "required" and not SKILL.match(rest) and not DEGREE_ALT.search(line):  # overall, and no degree-or-equivalent route
                floors.append(low)
    return min(floors) if floors else None
