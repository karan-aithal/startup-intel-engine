import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, href, params, region, type Sp } from "../lib/api";
import { Empty, Loading, Pager, RegionTabs } from "../lib/ui";

type Job = { id: string; title: string; location: string | null; published_at: string | null; source_url: string | null; source: string; company_id: string | null; company: string | null; seniority: string | null };
type Data = { total: number; items: Job[]; top_companies: { company: string | null; company_id: string | null; jobs: number }[] };

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const r = region(p);
  const d = await get<Data>("/api/hiring", { region: r, q: p.q, company_id: p.company_id, offset: p.offset });
  return (
    <>
      <RegionTabs path="/hiring" region={r} extra={{ q: p.q, company_id: p.company_id }} />
      <form className={styles.filters}>
        <input type="hidden" name="region" value={r} />
        {p.company_id && <input type="hidden" name="company_id" value={p.company_id} />}
        <input name="q" placeholder="Job title" defaultValue={p.q} />
        <button>Apply</button>
        {p.company_id && <Link href={href("/hiring", { region: r, q: p.q })}>Clear company filter</Link>}
      </form>
      {d.top_companies.length > 0 && (
        <p className={styles.muted}>Most jobs: {d.top_companies.slice(0, 8).map((c, i) => (
          <span key={i}>{i > 0 && ", "}{c.company_id ? <Link href={href("/hiring", { region: r, q: p.q, company_id: c.company_id })}>{c.company}</Link> : c.company ?? "unknown"} ({c.jobs})</span>
        ))}</p>
      )}
      {d.items.length ? (
        <div className={styles.scroll}>
          <table className={styles.table}><thead><tr><th>Title</th><th>Company</th><th>Location</th><th>Level</th><th>Published</th><th>Source</th></tr></thead>
            <tbody>{d.items.map((j) => (
              <tr key={j.id}><td>{j.source_url ? <a href={j.source_url}>{j.title}</a> : j.title}</td>
                <td>{j.company_id ? <Link href={`/companies/${j.company_id}`}>{j.company}</Link> : (j.company ?? "—")}</td>
                <td>{j.location ?? "—"}</td><td>{j.seniority ?? "—"}</td><td>{day(j.published_at)}</td><td>{j.source}</td></tr>
            ))}</tbody></table>
        </div>
      ) : <Empty what="jobs" />}
      <Pager path="/hiring" p={{ ...p, region: r }} total={d.total} />
    </>
  );
}

export default function Hiring({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Hiring</h1>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
