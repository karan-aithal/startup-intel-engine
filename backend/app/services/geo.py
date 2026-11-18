"""Where a job can be worked from: India, remote-and-open-to-India, or neither. One rule shared by alerts, the remote-board adapters and /api/explore.

Remote means the location text (or, when it is blank, the title) says so. A remote job counts only when its location names no place: once the
"remote" and "worldwide" words are taken out, nothing may be left. "Remote", "Remote (worldwide)" and "Remote, APAC" pass; "Remote, US",
"Remote - California" and "Europe (remote)" do not; "Remote, US or India" passes because India is named. An allowlist, so it needs no list of
countries and states. The remote rules follow career-ops (MIT, scan.mjs titleSignalsRemote and whole-label worldwide matching)."""
import re

INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata|calcutta|ahmedabad|jaipur|kochi|cochin|"
    r"chandigarh|mohali|coimbatore|indore|trivandrum|thiruvananthapuram|nagpur|lucknow|vadodara|bhubaneswar|visakhapatnam|mysuru|mysore)\b", re.IGNORECASE)
REMOTE = re.compile(r"\b(remote|remotely|work from home|wfh|anywhere|telecommut\w*)\b", re.IGNORECASE)
NOT_REMOTE = re.compile(r"\b(?:non|not|no)[^a-z]*remote", re.IGNORECASE)  # "Not remote", "non-remote": never rescued
WORLDWIDE = re.compile(r"\b(worldwide|anywhere|global|apac|asia(?:[- ]pacific)?|south asia|international)\b", re.IGNORECASE)
EXCEPT = re.compile(r"\b(except|excluding|outside|not|ex)\b", re.IGNORECASE)
# Words that describe the job or say "remote"/"worldwide" rather than name a place. Whatever else a remote location says is a place it is limited to.
# ponytail: a job whose location carries other words ("Remote - Contract role (Tier 2)") is treated as limited and hidden; add the word here when one slips out.
FILLER = re.compile(REMOTE.pattern + "|" + WORLDWIDE.pattern + r"|\b(fully|100|full[- ]?time|part[- ]?time|contract|permanent|position|role|job|"
                    r"work|home|from|or|and|the|in|only|eligible|open|to)\b", re.IGNORECASE)


def classify_geo(location: str | None, title: str | None = None) -> tuple[bool, bool]:
    """(india, remote_open_to_india). Both can be true ("Remote, India")."""
    loc = (location or "").strip()
    india = bool(INDIA.search(loc))
    if loc:
        marked = bool(REMOTE.search(loc)) and not NOT_REMOTE.search(loc)
    else:
        t = title or ""
        marked = bool(REMOTE.search(t)) and not NOT_REMOTE.search(t)  # the title only counts when the source gave no location
    unplaced = not re.sub(r"[\W_]+", " ", FILLER.sub(" ", loc)).strip()  # nothing but remote / worldwide words
    return india, marked and (india or unplaced or bool(WORLDWIDE.search(loc) and not EXCEPT.search(loc)))


def open_to_india(restriction: str | None) -> bool:
    """For boards that give a hiring-country restriction apart from the location (Remotive). Blank means no restriction; otherwise the text
    must name India or say worldwide/APAC/Asia (without "except")."""
    r = (restriction or "").strip()
    return not r or bool(INDIA.search(r) or (WORLDWIDE.search(r) and not EXCEPT.search(r)))


def wanted(location: str | None, title: str | None = None) -> bool:
    return any(classify_geo(location, title))
