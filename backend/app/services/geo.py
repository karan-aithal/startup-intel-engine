"""Where a job can be worked from: India, remote-and-open-to-India, or neither. One rule shared by alerts, the remote-board adapters and /api/explore.

Remote means the location text (or, when it is blank, the title) says so. A remote job counts only when its location names no place: once the
"remote" and "worldwide" words are taken out, nothing may be left. "Remote", "Remote (worldwide)" and "Remote, APAC" pass; "Remote, US",
"Remote - California" and "Europe (remote)" do not; "Remote, US or India" passes because India is named. An allowlist, so it needs no list of
countries and states. The remote rules follow career-ops (MIT, scan.mjs titleSignalsRemote and whole-label worldwide matching)."""
import re

INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata|calcutta|ahmedabad|jaipur|kochi|cochin|"
    r"chandigarh|mohali|coimbatore|indore|trivandrum|thiruvananthapuram|nagpur|lucknow|vadodara|bhubaneswar|visakhapatnam|mysuru|mysore)\b", re.IGNORECASE)
# More Indian places, for job locations only (J2, decision 105). Kept apart from INDIA, which the funding checks in verify.py and the dashboards also use:
# widening that would change funding evidence. Found on real data: Paytm and CRED list "Ludhiana, Punjab", "Patna, Bihar", "Aligarh, Uttar Pradesh" and
# "tamil nadu" with no word "India". States are safe words; ambiguous towns (Salem, Punjab alone) are left out on purpose.
PLACES = (r"|nashik|nasik|thane|navi mumbai|faridabad|ghaziabad|surat|rajkot|patna|ranchi|raipur|bhopal|dehradun|guwahati|mangalore|mangaluru|hubli|belagavi|"
          r"belgaum|madurai|trichy|tiruchirappalli|vijayawada|vizag|guntur|warangal|kanpur|agra|varanasi|ludhiana|amritsar|patiala|jalandhar|jodhpur|udaipur|"
          r"panaji|pondicherry|puducherry|gandhinagar|hosur|aurangabad|kolhapur|jamshedpur|cuttack|siliguri|durgapur|vellore|aligarh|sonipat|karnal|meerut|"
          r"prayagraj|allahabad|jammu|andhra pradesh|arunachal pradesh|assam|bihar|chhattisgarh|goa|gujarat|haryana|himachal pradesh|jharkhand|karnataka|"
          r"kerala|madhya pradesh|maharashtra|odisha|orissa|rajasthan|tamil nadu|telangana|uttar pradesh|uttarakhand|west bengal")
JOB_INDIA = re.compile(INDIA.pattern[:-3] + PLACES + r")\b", re.IGNORECASE)
REMOTE = re.compile(r"\b(remote|remotely|work from home|wfh|anywhere|telecommut\w*)\b", re.IGNORECASE)
NOT_REMOTE = re.compile(r"\b(?:non|not|no)[^a-z]*remote", re.IGNORECASE)  # "Not remote", "non-remote": never rescued
WORLDWIDE = re.compile(r"\b(worldwide|anywhere|global|apac|asia(?:[- ]pacific)?|south asia|international)\b", re.IGNORECASE)
EXCEPT = re.compile(r"\b(except|excluding|outside|not|ex)\b", re.IGNORECASE)
# Words that describe the job or say "remote"/"worldwide" rather than name a place. Whatever else a remote location says is a place it is limited to.
# ponytail: a job whose location carries other words ("Remote - Contract role (Tier 2)") is treated as limited and hidden; add the word here when one slips out.
FILLER = re.compile(REMOTE.pattern + "|" + WORLDWIDE.pattern + r"|\b(fully|100|full[- ]?time|part[- ]?time|contract|permanent|position|role|job|"
                    r"work|home|from|or|and|the|in|only|eligible|open|to)\b", re.IGNORECASE)


HYBRID_LOC = re.compile(r"\b(?:hybrid|on[- ]?site|in[- ]office|office[- ]based)\b", re.IGNORECASE)  # the location text itself says it is not remote
NEGATOR = re.compile(r"\b(?:except|excluding|excluded|outside|not|ex|other\s+than|besides)\b[^;,)]{0,30}$", re.IGNORECASE)
NEGATED_AFTER = re.compile(r"^[^;,(]{0,12}\b(?:excluded|not\s+(?:eligible|available|allowed))\b", re.IGNORECASE)


def names_india(loc: str) -> bool:
    """An Indian place is named and not excluded: "Remote Worldwide except India", "Remote, outside India" and "India excluded" do not count.
    Whitespace and dash variants are folded first (job boards ship no-break spaces and en dashes; career-ops fixed the same in its location filter)."""
    return any(not (NEGATOR.search(loc[:m.start()]) or NEGATED_AFTER.match(loc[m.end():])) for m in JOB_INDIA.finditer(loc))


def classify_geo(location: str | None, title: str | None = None, workplace: str | None = None) -> tuple[bool, bool]:
    """(india, remote_open_to_india). Both can be true ("Remote, India").
    `workplace` is the ATS's own remote | hybrid | onsite field (Lever, Ashby; official docs). It settles the ambiguous cases only: hybrid or onsite is never
    remote, and a remote job with no location is remote. It never widens a named place: "Remote" + workplace remote + "California" is still limited."""
    loc = re.sub(r"[\s\u00a0\u2007\u202f]+", " ", re.sub(r"[\u2010-\u2015\u2212]", "-", (location or ""))).strip()
    india = names_india(loc)
    if workplace in ("hybrid", "onsite") or HYBRID_LOC.search(loc):
        marked = False
    elif workplace == "remote" and not NOT_REMOTE.search(loc):
        marked = True
    elif loc:
        marked = bool(REMOTE.search(loc)) and not NOT_REMOTE.search(loc)
    else:
        t = title or ""
        marked = bool(REMOTE.search(t)) and not NOT_REMOTE.search(t)  # the title only counts when the source gave no location
    unplaced = not re.sub(r"[\W_]+", " ", FILLER.sub(" ", loc)).strip()  # nothing but remote / worldwide words
    named = JOB_INDIA.search(loc) and not india  # India is named only to be excluded: never open to India
    return india, marked and not named and (india or unplaced or bool(WORLDWIDE.search(loc) and not EXCEPT.search(loc)))


def open_to_india(restriction: str | None) -> bool:
    """For boards that give a hiring-country restriction apart from the location (Remotive). Blank means no restriction; otherwise the text
    must name India or say worldwide/APAC/Asia (without "except")."""
    r = (restriction or "").strip()
    return not r or bool(INDIA.search(r) or (WORLDWIDE.search(r) and not EXCEPT.search(r)))


def wanted(location: str | None, title: str | None = None, workplace: str | None = None) -> bool:
    return any(classify_geo(location, title, workplace))
