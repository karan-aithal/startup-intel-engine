import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, href, params, type Sp } from "../lib/api";
import { Empty, Loading, Pager, Pill } from "../lib/ui";
import { New, Visit } from "./new";
import { Schedule } from "./schedule";
import { Track } from "./track";

type Job = {
  id: string; title: string | null; location: string | null; published_at: string | null; detected_at: string | null; source_url: string | null;
  source: string; company_id: string | null; company: string | null; india: boolean; remote: boolean;
  fit: "strong" | "related" | "weak"; role: string; seniority: string | null; open: boolean | null;
  funding: { stage: string | null; status: string; at: string | null } | null;
  years_min: number | null; above_band: boolean; reposted: boolean; first_seen_at: string | null; tracked: string | null;
};
type Saved = { id: string; name: string; status: string; filters: Record<string, string | number> };
type Counts = { all: number; india: number; remote: number };
type Data = { total: number; items: Job[]; truncated: boolean; counts: Counts; sources: { source: string; jobs: number }[] };

const MODES = [["all", "India + remote"], ["india", "India"], ["remote", "Remote"]] as const;
const SORTS = [["newest", "Newest"], ["fit", "Best fit"], ["funded", "Recently funded"], ["company", "Company"]] as const;
const DAYS = [["1", "24h"], ["3", "3d"], ["7", "7d"], ["14", "14d"], ["30", "30d"]] as const;

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const mode = MODES.find(([m]) => m === p.mode)?.[0] ?? "all";
  const days = DAYS.find(([d]) => d === p.days)?.[0] ?? "7";
  const sort = SORTS.find(([v]) => v === p.sort)?.[0] ?? "newest";
  const status = p.status === "all" ? "all" : "open";
  const tracked = p.tracked === "show" ? "show" : "hide";  // jobs you saved or skipped are hidden by default; they live in the Pipeline
  const keep = { q: p.q, exclude: p.exclude, location: p.location, source: p.source, days, sort, status, tracked };
  const d = await get<Data>("/api/explore", { mode, ...keep, offset: p.offset });
  const saved = await get<{ searches: Saved[] }>("/api/scans").then((x) => x.searches).catch(() => [] as Saved[]);  // chips are a convenience: Explore works without them
  return (
    <>
      <div className={styles.filters}>
        <div className={styles.tabs}>
          {MODES.map(([m, label]) => (
            <Link key={m} href={href("/explore", { ...keep, mode: m })} className={m === mode ? styles.on : ""}>{label} ({d.counts[m]})</Link>
          ))}
        </div>
        <div className={styles.tabs}>
          {DAYS.map(([v, label]) => (
            <Link key={v} href={href("/explore", { ...keep, days: v, mode })} className={v === days ? styles.on : ""}>{label}</Link>
          ))}
        </div>
        <div className={styles.tabs}>
          {SORTS.map(([v, label]) => (
            <Link key={v} href={href("/explore", { ...keep, sort: v, mode })} className={v === sort ? styles.on : ""}>{label}</Link>
          ))}
        </div>
        <div className={styles.tabs}>
          <Link href={href("/explore", { ...keep, status: status === "open" ? "all" : "open", mode })}>{status === "open" ? "Open only · show closed" : "Including closed · open only"}</Link>
        </div>
        <div className={styles.tabs}>
          <Link href={href("/explore", { ...keep, tracked: tracked === "hide" ? "show" : "hide", mode })}>{tracked === "hide" ? "Hiding tracked · show them" : "Showing tracked · hide them"}</Link>
          <Link href="/pipeline">Pipeline →</Link>
        </div>
      </div>
      <Visit />
      {saved.length > 0 && (
        <div className={styles.tabs}>
          {saved.map((s) => <Link key={s.id} href={href("/explore", { ...s.filters })} title={`Saved search (${s.status})`}>{s.name}</Link>)}
          <Link href="/scans">Scans →</Link>
        </div>
      )}
      <Schedule filters={{ mode, q: p.q ?? "", exclude: p.exclude ?? "", location: p.location ?? "", source: p.source ?? "", days, sort, status }} />
      <form className={styles.filters}>
        <input type="hidden" name="mode" value={mode} />
        <input type="hidden" name="days" value={days} />
        <input type="hidden" name="sort" value={sort} />
        <input type="hidden" name="status" value={status} />
        <input type="hidden" name="tracked" value={tracked} />
        <input name="q" placeholder="Roles: backend, data engineer…" defaultValue={p.q} aria-label="Roles to find" />
        <input name="exclude" placeholder="Exclude: manager, sales…" defaultValue={p.exclude} aria-label="Exclude" />
        <input name="location" placeholder="Location: Bengaluru, worldwide…" defaultValue={p.location} aria-label="City or location" />
        <select name="source" defaultValue={p.source ?? ""} aria-label="Source">
          <option value="">All sources</option>
          {d.sources.map((s) => <option key={s.source} value={s.source}>{s.source} ({s.jobs})</option>)}
        </select>
        <button>Search</button>
      </form>
      {d.items.length ? (
        <div className={styles.scroll}>
          <table className={styles.table}><thead><tr><th>Title</th><th>Company</th><th>Location</th><th>Fit</th><th>Role</th><th>Funding</th><th>Posted</th><th>Source</th><th>Pipeline</th></tr></thead>
            <tbody>{d.items.map((j) => (
              <tr key={j.id}>
                <td>{j.source_url ? <a href={j.source_url} rel="noopener noreferrer">{j.title ?? "(untitled)"}</a> : (j.title ?? "(untitled)")}<New at={j.first_seen_at ?? j.detected_at} />{j.open === false && <> <Pill v="Closed" /></>}{j.reposted && <> <Pill v="Reposted" /></>}</td>
                <td>{j.company_id ? <Link href={`/companies/${j.company_id}`}>{j.company}</Link> : (j.company ?? "—")}</td>
                <td>{j.location ?? "—"}</td>
                <td><Pill v={j.fit} /> {j.india && <Pill v="India" />} {j.remote && <Pill v="Remote" />}</td>
                <td>{j.role}{j.seniority ? ` · ${j.seniority}` : ""}{j.years_min ? ` · ${j.years_min}+ yrs` : ""}{j.above_band && <> <Pill v="Above band" /></>}</td>
                <td>{j.funding ? <Pill v={`${j.funding.stage ?? "Round"} ${day(j.funding.at)}`} /> : "—"}</td>
                <td>{day(j.published_at ?? j.detected_at)}</td><td>{j.source}</td><td><Track id={j.id} tracked={j.tracked} /></td>
              </tr>
            ))}</tbody></table>
        </div>
      ) : <Empty what="jobs" />}
      {d.truncated && <p className={styles.muted}>Showing the newest 5000 matching postings; narrow the date window to see older ones.</p>}
      <Pager path="/explore" p={{ ...p, mode, days }} total={d.total} />
    </>
  );
}

export default function Explore({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Explore</h1>
      <p className={styles.muted}>Jobs in India, and remote jobs open to India, from company job boards, Himalayas and Remotive. Remote jobs limited to other countries are hidden.</p>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
