"""What config/jobs.yaml is doing to your jobs (J5): which role keywords earn their keep, and what falls through. Read-only, zero tokens.
Idea and the `unique` metric from career-ops filter-audit.mjs (MIT): a keyword with many hits but 0 unique can be deleted without changing any job's role,
because another keyword in the same group already catches every one of them. A keyword with few hits that are all unique is the only thing catching them.
Usage: cd backend && uv run python ../scripts/jobs_keyword_audit.py [--top 25]"""
import asyncio
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text

from app.db.session import get_engine, get_sessionmaker
from app.services import jobfit

STOP = set(["a", "an", "and", "at", "for", "in", "of", "on", "or", "the", "to", "with", "de", "la", "en", "-", "&", "/", "senior", "junior", "sr", "jr", "lead", "staff", "principal", "i", "ii", "iii", "iv", "remote", "hybrid", "new"])


async def main(top: int) -> None:
    async with get_sessionmaker()() as s:
        titles = [r[0] or "" for r in await s.execute(text("SELECT title FROM job_postings WHERE closed_at IS NULL"))]
    await get_engine().dispose()
    roles = jobfit.cfg()["role"]
    rx = {(g, w): re.compile(r"\b" + jobfit.word_pattern(w), re.IGNORECASE) for g, ws in roles.items() for w in ws}
    hits, unique, per_role = collections.Counter(), collections.Counter(), collections.Counter()
    other = collections.Counter()
    for t in titles:
        matched = [k for k, r in rx.items() if r.search(t)]
        for k in matched:
            hits[k] += 1
        for g in roles:  # a keyword is unique when it is the only one in its group that matches this title
            in_group = [k for k in matched if k[0] == g]
            if len(in_group) == 1:
                unique[in_group[0]] += 1
        role = jobfit.role_category(t)
        per_role[role] += 1
        if role == "other":
            other.update(w for w in re.findall(r"[a-z]{3,}", t.lower()) if w not in STOP)
    print(f"{len(titles)} open jobs.\n\nRole shares: " + ", ".join(f"{k} {n} ({n / len(titles):.0%})" for k, n in per_role.most_common()))
    no_level = sum(1 for t in titles if jobfit.seniority(t) is None)
    print(f"No seniority marker in the title: {no_level} ({no_level / len(titles):.0%})\n")
    print(f"{'group':14} {'keyword':22} {'hits':>6} {'unique':>7}")
    for k in sorted(rx, key=lambda k: (hits[k] == 0, unique[k] == 0, -hits[k])):
        flag = "  <- never matches" if hits[k] == 0 else "  <- adds nothing (another keyword in the group already catches every hit)" if unique[k] == 0 else ""
        print(f"{k[0]:14} {k[1]:22} {hits[k]:>6} {unique[k]:>7}{flag}")
    print("\nWords most common in titles filed under 'other' (candidates to add to a group):\n  " + ", ".join(f"{w} {n}" for w, n in other.most_common(top)))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 25))
