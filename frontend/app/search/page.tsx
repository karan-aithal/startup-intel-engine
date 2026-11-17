import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, params, region, type Sp } from "../lib/api";
import { Loading, RegionTabs } from "../lib/ui";

type Data = {
  companies: { id: string; name: string; website_url: string | null }[];
  events: { id: string; event_type: string; title: string | null; source_url: string | null; published_at: string | null; company_name: string | null; location: string | null; source: string }[];
};

async function Results({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const r = region(p);
  if ((p.q ?? "").length < 2) return <p className={styles.muted}>Type at least 2 characters.</p>;
  const d = await get<Data>("/api/search", { q: p.q, region: r });
  return (
    <>
      <RegionTabs path="/search" region={r} extra={{ q: p.q }} />
      <h2>Companies ({d.companies.length})</h2>
      {d.companies.length ? <ul>{d.companies.map((c) => <li key={c.id}><Link href={`/companies/${c.id}`}>{c.name}</Link></li>)}</ul> : <p className={styles.muted}>No matching companies.</p>}
      <h2>Events ({d.events.length})</h2>
      {d.events.length ? (
        <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Title</th><th>Type</th><th>Source</th><th>Location</th><th>Published</th></tr></thead>
          <tbody>{d.events.map((e) => (
            <tr key={e.id}><td>{e.source_url ? <a href={e.source_url}>{e.title ?? e.source_url}</a> : e.title}</td><td>{e.event_type}</td><td>{e.source}</td><td>{e.location ?? "—"}</td><td>{day(e.published_at)}</td></tr>
          ))}</tbody></table></div>
      ) : <p className={styles.muted}>No matching events.</p>}
    </>
  );
}

async function Box({ sp }: { sp: Sp }) {
  const p = await params(sp);
  return (
    <form className={styles.filters}>
      <input name="q" placeholder="Search companies, funding and job events" defaultValue={p.q} style={{ minWidth: "22rem" }} autoFocus />
      <input type="hidden" name="region" value={region(p)} />
      <button>Search</button>
    </form>
  );
}

export default function Search({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Search</h1>
      <Suspense fallback={<Loading />}><Box sp={searchParams} /><Results sp={searchParams} /></Suspense>
    </main>
  );
}
