import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, href, params, type Sp } from "../lib/api";
import { Empty, Loading, Pager } from "../lib/ui";
import { Table, type Row } from "./table";

type Job = {
  id: string; title: string | null; location: string | null; published_at: string | null; detected_at: string | null; source_url: string | null; source: string;
  company_id: string | null; company: string | null; india: boolean; remote: boolean; fit: string; open: boolean | null; reposted: boolean;
  funding: { stage: string | null; at: string | null } | null;
};
type Tracked = {
  id: string; title: string; url: string | null; company_id: string | null; company_name: string | null; location: string | null; status: string;
  next_step: string | null; salary_text: string | null; applied_at: string | null; status_changed_at: string; open: boolean | null;
};
type Data = {
  tab: string; total: number; truncated: boolean; items: (Job & Tracked)[];
  counts: Record<string, number>; funnel: { stage: string; reached: number }[];
};

const TABS = ["inbox", "saved", "applied", "responded", "assessment", "interview", "offer", "hired", "rejected", "withdrawn", "skipped"];
const SORTS: Record<string, [string, string][]> = {
  inbox: [["newest", "Newest"], ["fit", "Best fit"], ["funded", "Recently funded"], ["company", "Company"]],
  tracked: [["updated", "Last change"], ["applied", "Applied date"], ["company", "Company"], ["title", "Title"]],
};
const MODES = [["all", "India + remote"], ["india", "India"], ["remote", "Remote"]] as const;
const DAYS = [["1", "24h"], ["3", "3d"], ["7", "7d"], ["14", "14d"], ["30", "30d"]] as const;
const label = (t: string) => t[0].toUpperCase() + t.slice(1);

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const tab = TABS.includes(p.tab) ? p.tab : "inbox";
  const inbox = tab === "inbox";
  const sorts = SORTS[inbox ? "inbox" : "tracked"];
  const sort = sorts.find(([v]) => v === p.sort)?.[0] ?? sorts[0][0];
  const mode = MODES.find(([m]) => m === p.mode)?.[0] ?? "all";
  const days = DAYS.find(([d]) => d === p.days)?.[0] ?? "7";
  const keep = { tab, q: p.q, sort, mode: inbox ? mode : undefined, days: inbox ? days : undefined };
  const d = await get<Data>("/api/pipeline", { ...keep, offset: p.offset });
  const rows: Row[] = d.items.map((j) => inbox
    ? { id: j.id, title: j.title ?? "(untitled)", url: j.source_url, company: j.company, companyId: j.company_id, location: j.location, detail: false, open: j.open,
        badges: [j.fit, ...(j.india ? ["India"] : []), ...(j.remote ? ["Remote"] : []), ...(j.reposted ? ["Reposted"] : [])],
        a: j.funding ? `${j.funding.stage ?? "Round"} ${day(j.funding.at)}` : "—", b: `${day(j.published_at ?? j.detected_at)} · ${j.source}` }
    : { id: j.id, title: j.title, url: j.url, company: j.company_name, companyId: j.company_id, location: j.location, detail: true, open: j.open, badges: [],
        a: [j.next_step, j.salary_text].filter(Boolean).join(" · ") || "—", b: `${j.applied_at ? `applied ${day(j.applied_at)} · ` : ""}changed ${day(j.status_changed_at)}` });
  return (
    <>
      <div className={styles.grid}>
        {d.funnel.map((f) => <div key={f.stage} className={styles.stat}><b>{f.reached}</b><span>Reached {f.stage}</span></div>)}
      </div>
      <div className={styles.filters}>
        <div className={styles.tabs}>
          {TABS.map((t) => <Link key={t} href={href("/pipeline", { tab: t })} className={t === tab ? styles.on : ""} aria-current={t === tab ? "page" : undefined}>{label(t)} ({d.counts[t] ?? 0})</Link>)}
        </div>
      </div>
      <div className={styles.filters}>
        <div className={styles.tabs}>
          {sorts.map(([v, l]) => <Link key={v} href={href("/pipeline", { ...keep, sort: v })} className={v === sort ? styles.on : ""} aria-current={v === sort ? "page" : undefined}>{l}</Link>)}
        </div>
        {inbox && <div className={styles.tabs}>{MODES.map(([m, l]) => <Link key={m} href={href("/pipeline", { ...keep, mode: m })} className={m === mode ? styles.on : ""} aria-current={m === mode ? "page" : undefined}>{l}</Link>)}</div>}
        {inbox && <div className={styles.tabs}>{DAYS.map(([v, l]) => <Link key={v} href={href("/pipeline", { ...keep, days: v })} className={v === days ? styles.on : ""} aria-current={v === days ? "page" : undefined}>{l}</Link>)}</div>}
      </div>
      <form className={styles.filters}>
        <input type="hidden" name="tab" value={tab} />
        <input type="hidden" name="sort" value={sort} />
        {inbox && <><input type="hidden" name="mode" value={mode} /><input type="hidden" name="days" value={days} /></>}
        <input name="q" placeholder={inbox ? "Roles: backend, data engineer…" : "Title, company or note"} defaultValue={p.q} aria-label="Search" />
        <button>Search</button>
      </form>
      {inbox && <p className={styles.muted}>Jobs from <Link href="/explore">Explore</Link> you have not saved or skipped yet ({days === "1" ? "24 hours" : `${days} days`}). Saving or skipping moves a job out of here.</p>}
      {rows.length
        ? <Table key={`${tab}${p.offset}${p.q}`} rows={rows} inbox={inbox} heads={inbox ? ["Funding", "Posted · source"] : ["Next step · salary", "Dates"]} />
        : <Empty what={inbox ? "jobs in the Inbox" : `${tab} jobs`} />}
      {inbox && d.truncated && <p className={styles.muted}>Reading the newest 5000 matching postings; narrow the date window to see older ones.</p>}
      <Pager path="/pipeline" p={{ ...keep, offset: p.offset ?? "" } as Record<string, string>} total={d.total} />
    </>
  );
}

export default function Pipeline({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Pipeline</h1>
      <p className={styles.muted}>Track jobs from saved to hired. This page only records what you did: it never applies, sends or submits anything.</p>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
