"""Phase 13: company identity changes the owner approved in docs/quality/identity_merge_map.csv: merge, rename, not-a-company.

Nothing is ever deleted. A merge repoints rows (company_id) from the source to the survivor and hides the source; a rename keeps the old
name as an alias so later mentions still resolve; "not a company" only hides it (rounds and evidence stay recorded). Every change writes an
audit row in company_merges so it can be undone. After a merge the survivor's rounds are rebuilt by the normal verification pass, which is
what collapses two reports of one round into one. Sent alerts, ratings and discovery candidates are records and are never repointed."""
import csv
import json
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.services import quality
from app.services.companies import canonical
from app.services.verify import verify_company

DERIVED = {"funding_rounds", "evidence", "company_investors"}  # rebuilt from claims by the verification pass
SKIP = {"alert_deliveries", "feedback"}  # records of what was sent or rated; quality.REF_TABLES already leaves out discovery candidates
ACTIONS = {"merge into": "merge", "rename to": "rename", "not_a_company": "exclude"}
CELL = re.compile(r"^\s*yes\s*:\s*(merge into|rename to|not_a_company)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
NAME = re.compile(r"^(.*?)(?:\s*\(|\.\s|\.$|,|$)", re.DOTALL)


def parse_decision(cell: str) -> tuple[str, str] | None:
    """The owner's approve cell -> (action, name or detail). None for blank or "no". A "yes" that says nothing we can act on raises."""
    c = (cell or "").strip()
    if not c or c.lower().startswith("no"):
        return None
    m = CELL.match(c)
    if not m:
        raise ValueError(f"cannot read the approve cell: {c[:80]!r}")
    action, rest = ACTIONS[m.group(1).lower()], m.group(2).strip()
    if action == "exclude":
        return action, rest.lstrip(", ").split("(")[0].strip()[:120]
    name = NAME.match(rest).group(1).strip()
    if not name:
        raise ValueError(f"no company name after {m.group(1)!r}: {c[:80]!r}")
    return action, name


def read_decisions(path: Path) -> list[dict]:
    out = []
    for i, r in enumerate(csv.DictReader(path.open(newline="")), start=2):  # start=2: the spreadsheet row, header is row 1
        if (p := parse_decision(r.get("approve", ""))):
            out.append({"row": i, "company_id": r["company_id"], "company": r["company"], "action": p[0], "arg": p[1], "decision": r["approve"].strip()})
    return out


async def _ref_tables(s) -> list[tuple[str, str]]:
    return [(t, c) for t, c in (await s.execute(text(quality.REF_TABLES))).all() if t not in SKIP]


async def resolve(s, decisions: list[dict]) -> list[str]:
    """Problems that must stop the run before anything is written. Fills d['target_id'] for merges."""
    errors, sources = [], {d["company_id"] for d in decisions if d["action"] in ("merge", "exclude")}
    for d in decisions:
        row = (await s.execute(text("SELECT name, excluded_reason FROM companies WHERE id = CAST(:i AS uuid)"), {"i": d["company_id"]})).first()
        if not row:
            errors.append(f"row {d['row']}: {d['company']} no longer exists"); continue
        if row.name != d["company"]:
            errors.append(f"row {d['row']}: the map says {d['company']!r} but the company is now {row.name!r}"); continue
        if (await s.execute(text("SELECT 1 FROM company_merges WHERE company_id = CAST(:i AS uuid) AND undone_at IS NULL"), {"i": d["company_id"]})).first():
            d["skip"] = "already applied"; continue
        if d["action"] == "merge":
            t = (await s.execute(text("SELECT id::text FROM companies WHERE lower(name) = lower(:n) AND id <> CAST(:i AS uuid) AND excluded_reason IS NULL"),
                                 {"n": d["arg"], "i": d["company_id"]})).scalars().all()
            if len(t) != 1:
                errors.append(f"row {d['row']}: merge into {d['arg']!r} matches {len(t)} companies, need exactly 1"); continue
            if t[0] in sources:
                errors.append(f"row {d['row']}: {d['arg']!r} is itself merged away or excluded in this map"); continue
            d["target_id"] = t[0]
        elif d["action"] == "rename":
            c = canonical(d["arg"])
            if (await s.execute(text("""SELECT 1 FROM companies WHERE canonical_name = :c AND id <> CAST(:i AS uuid)
                UNION ALL SELECT 1 FROM company_aliases WHERE alias = :c AND company_id <> CAST(:i AS uuid)"""), {"c": c, "i": d["company_id"]})).first():
                errors.append(f"row {d['row']}: rename to {d['arg']!r} collides with another company; stopped, decide merge or a different name")
    return errors


class MergeLoss(RuntimeError):
    """A merge would lose evidence, an investor link or an alert's round link. The caller's transaction is rolled back."""


async def _alerts_on(s, rounds: list[str]) -> dict[str, str]:
    return {a: r for a, r in (await s.execute(text("SELECT id::text, funding_round_id::text FROM alert_deliveries WHERE funding_round_id = ANY(CAST(:r AS uuid[]))"),
                                              {"r": rounds})).all()}


async def _derived(s, ids: list[str]) -> dict[str, dict[str, dict]]:
    """{table: {row id: row as json}} for the rounds, evidence and investor links of these companies, to compare around a merge."""
    q = {"funding_rounds": "SELECT id::text, to_jsonb(t) FROM funding_rounds t WHERE company_id = ANY(CAST(:c AS uuid[]))",
         "evidence": """SELECT id::text, to_jsonb(t) FROM evidence t WHERE company_id = ANY(CAST(:c AS uuid[]))
            OR funding_round_id IN (SELECT id FROM funding_rounds WHERE company_id = ANY(CAST(:c AS uuid[])))""",
         "company_investors": "SELECT id::text, to_jsonb(t) FROM company_investors t WHERE company_id = ANY(CAST(:c AS uuid[]))"}
    return {t: {i: j for i, j in (await s.execute(text(sql), {"c": ids})).all()} for t, sql in q.items()}


async def _check_nothing_lost(before: dict, after: dict, alerts_before: dict[str, str]) -> dict:
    """Verification may drop a duplicate round and rebuild its evidence and links from the claims. That is fine only if every piece of
    evidence and every investor link still exists in equivalent form, and no sent alert pointed at a dropped round. Returns what was dropped
    (saved in the audit row); raises MergeLoss otherwise."""
    gone = {t: [before[t][i] for i in before[t].keys() - after[t].keys()] for t in before}
    ev_key = lambda r: (r["source_event_id"], r["evidence_type"], r["supports"])
    inv_key = lambda r: (r["investor_id"], r["role"])
    kept_ev, kept_inv = {ev_key(r) for r in after["evidence"].values()}, {inv_key(r) for r in after["company_investors"].values()}
    lost = [("evidence", r) for r in gone["evidence"] if ev_key(r) not in kept_ev] + [("investor link", r) for r in gone["company_investors"] if inv_key(r) not in kept_inv]
    if lost:
        raise MergeLoss(f"would lose {len(lost)} piece(s) of evidence or investor links, e.g. {lost[0][0]} {lost[0][1].get('id')}")
    cut = [a for a, r in alerts_before.items() if r in {x["id"] for x in gone["funding_rounds"]}]  # the FK sets these to NULL when a round is deleted
    if cut:
        raise MergeLoss(f"{len(cut)} sent alert(s) point at a round the merge would collapse")
    return gone


async def _alias(s, company_id: str, alias: str) -> list[str]:
    r = (await s.execute(text("""INSERT INTO company_aliases (company_id, alias, confidence) VALUES (CAST(:c AS uuid), :a, 100)
        ON CONFLICT (company_id, alias) DO NOTHING RETURNING id::text"""), {"c": company_id, "a": alias})).scalars().all()
    return list(r)


async def _rounds(s, company_id: str) -> list[str]:
    q = await s.execute(text("""SELECT coalesce(stage, '-') || ' ' || coalesce(amount_original::text, '?') || ' ' || coalesce(currency, '') || ' ' || verification_status
        FROM funding_rounds WHERE company_id = CAST(:c AS uuid) ORDER BY announced_at NULLS LAST"""), {"c": company_id})
    return list(q.scalars())


async def apply_one(s, d: dict) -> dict:
    """One approved change plus its audit row. The caller commits (apply) or rolls back (dry run)."""
    src = d["company_id"]
    old = (await s.execute(text("SELECT name, canonical_name FROM companies WHERE id = CAST(:i AS uuid)"), {"i": src})).one()
    moved: dict = {}
    left: dict = {}
    created: dict = {}
    dropped: dict = {}
    report = {"row": d["row"], "action": d["action"], "company": d["company"], "arg": d["arg"]}
    if d["action"] == "merge":
        tgt = d["target_id"]
        report["rounds_before"] = {"source": await _rounds(s, src), "survivor": await _rounds(s, tgt)}
        before = await _derived(s, [src, tgt])
        alerts_before = await _alerts_on(s, list(before["funding_rounds"]))
        for tbl, col in await _ref_tables(s):
            for rid in (await s.execute(text(f"SELECT id::text FROM {tbl} WHERE {col} = CAST(:c AS uuid)"), {"c": src})).scalars().all():  # tbl and col come from the catalogue
                try:
                    async with s.begin_nested():  # a unique-key clash leaves that one row where it is instead of failing the merge
                        await s.execute(text(f"UPDATE {tbl} SET {col} = CAST(:t AS uuid) WHERE id = CAST(:i AS uuid)"), {"t": tgt, "i": rid})
                    moved.setdefault(tbl, []).append(rid)
                except IntegrityError:
                    left.setdefault(tbl, []).append(rid)
        if ids := await _alias(s, tgt, old.canonical_name):
            created["company_aliases"] = ids
        name = (await s.execute(text("SELECT name FROM companies WHERE id = CAST(:i AS uuid)"), {"i": tgt})).scalar_one()
        await s.execute(text("UPDATE companies SET excluded_reason = :r, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"r": f"merged into {name}", "i": src})
        report["verification"] = await verify_company(s, tgt)  # rebuilds the survivor's rounds: duplicate reports of one round collapse here
        report["rounds_after"] = await _rounds(s, tgt)
        dropped = await _check_nothing_lost(before, await _derived(s, [src, tgt]), alerts_before)
        report["collapsed_rounds"] = len(dropped["funding_rounds"])
    elif d["action"] == "rename":
        await s.execute(text("UPDATE companies SET name = :n, canonical_name = :c, updated_at = now() WHERE id = CAST(:i AS uuid)"),
                        {"n": d["arg"], "c": canonical(d["arg"]), "i": src})
        if ids := await _alias(s, src, old.canonical_name):
            created["company_aliases"] = ids
    else:
        await s.execute(text("UPDATE companies SET excluded_reason = :r, updated_at = now() WHERE id = CAST(:i AS uuid)"),
                        {"r": "not a company" + (f": {d['arg']}" if d["arg"] else ""), "i": src})
    report["moved"] = {k: len(v) for k, v in moved.items()}
    report["left_behind"] = {k: len(v) for k, v in left.items()}
    await s.execute(text("""INSERT INTO company_merges (action, company_id, target_id, old_name, old_canonical_name, new_name, decision, moved, left_behind, created, deleted)
        VALUES (:a, CAST(:c AS uuid), CAST(:t AS uuid), :on, :oc, :nn, :d, CAST(:m AS jsonb), CAST(:l AS jsonb), CAST(:cr AS jsonb), CAST(:de AS jsonb))"""),
        {"a": d["action"], "c": src, "t": d.get("target_id"), "on": old.name, "oc": old.canonical_name, "nn": d["arg"] if d["action"] == "rename" else None,
         "d": d["decision"], "m": json.dumps(moved), "l": json.dumps(left), "cr": json.dumps(created), "de": json.dumps(dropped)})
    return report


async def undo(s, merge_id: str) -> dict:
    """Reverse one audited change. Rows that verification deleted meanwhile (a duplicate round) are rebuilt from the claims by the next pass."""
    m = (await s.execute(text("SELECT * FROM company_merges WHERE id = CAST(:i AS uuid)"), {"i": merge_id})).mappings().first()
    if not m:
        raise ValueError(f"no such change {merge_id}")
    if m["undone_at"]:
        raise ValueError("already undone")
    ids = [str(x) for x in (m["company_id"], m["target_id"]) if x]
    if (await s.execute(text("""SELECT 1 FROM company_merges WHERE created_at > :t AND undone_at IS NULL AND id <> CAST(:i AS uuid)
            AND (company_id = ANY(CAST(:c AS uuid[])) OR target_id = ANY(CAST(:c AS uuid[])))"""), {"t": m["created_at"], "i": merge_id, "c": ids})).first():
        raise ValueError("a later change touches the same company: undo that one first")
    src, restored = str(m["company_id"]), {}
    now = (await s.execute(text("SELECT name, excluded_reason FROM companies WHERE id = CAST(:i AS uuid)"), {"i": src})).one()
    if m["action"] == "rename" and now.name != m["new_name"]:
        raise ValueError(f"the company has been renamed again since ({now.name!r}); undo leaves that name alone")
    if m["action"] != "rename" and not (now.excluded_reason or "").startswith("merged into" if m["action"] == "merge" else "not a company"):
        raise ValueError("the company's hidden state has been changed since; undo leaves it alone")
    if m["action"] == "merge":
        tgt, moved = str(m["target_id"]), m["moved"] or {}
        # Source-of-truth rows go back as they were. Rounds, evidence and investor links are derived from the claims, so they go back only when
        # no claim left at the survivor still points at them; everything else is rebuilt by the verification pass below.
        for tbl in [t for t in moved if t not in DERIVED]:
            r = await s.execute(text(f"UPDATE {tbl} SET company_id = CAST(:s AS uuid) WHERE id = ANY(CAST(:r AS uuid[])) AND company_id = CAST(:t AS uuid)"),  # tbl is a catalogue name saved by apply
                                {"s": src, "r": moved[tbl], "t": tgt})
            restored[tbl] = f"{r.rowcount} of {len(moved[tbl])}"
        kept = [str(x) for x in (await s.execute(text("""SELECT id FROM funding_rounds f WHERE id = ANY(CAST(:r AS uuid[])) AND company_id = CAST(:t AS uuid)
            AND NOT EXISTS (SELECT 1 FROM funding_round_claims c WHERE c.funding_round_id = f.id AND c.company_id = CAST(:t AS uuid))"""),
                                              {"r": moved.get("funding_rounds", []), "t": tgt})).scalars()]
        for tbl, rows in (("funding_rounds", kept), ("evidence", moved.get("evidence", [])), ("company_investors", moved.get("company_investors", []))):
            col = "id" if tbl == "funding_rounds" else "funding_round_id"
            cond = "" if tbl == "funding_rounds" else f" AND ({col} IS NULL OR {col} = ANY(CAST(:k AS uuid[])))"
            r = await s.execute(text(f"UPDATE {tbl} SET company_id = CAST(:s AS uuid) WHERE id = ANY(CAST(:r AS uuid[])) AND company_id = CAST(:t AS uuid){cond}"),
                                {"s": src, "r": rows, "t": tgt, "k": kept})
            restored[tbl] = f"{r.rowcount} of {len(moved.get(tbl, []))}"
        await s.execute(text("""UPDATE funding_round_claims SET funding_round_id = NULL WHERE id = ANY(CAST(:r AS uuid[])) AND company_id = CAST(:s AS uuid)
            AND NOT (funding_round_id = ANY(CAST(:k AS uuid[])))"""), {"r": moved.get("funding_round_claims", []), "s": src, "k": kept})
    if (m["created"] or {}).get("company_aliases"):
        await s.execute(text("DELETE FROM company_aliases WHERE id = ANY(CAST(:r AS uuid[]))"), {"r": m["created"]["company_aliases"]})
    if m["action"] == "rename":
        await s.execute(text("UPDATE companies SET name = :n, canonical_name = :c, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"n": m["old_name"], "c": m["old_canonical_name"], "i": src})
    else:
        await s.execute(text("UPDATE companies SET excluded_reason = NULL, updated_at = now() WHERE id = CAST(:i AS uuid)"), {"i": src})
    await s.execute(text("UPDATE company_merges SET undone_at = now() WHERE id = CAST(:i AS uuid)"), {"i": merge_id})
    if m["action"] == "merge":
        for c in ids:
            await verify_company(s, c)
    return {"action": m["action"], "restored": restored}
