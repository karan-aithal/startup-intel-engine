import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, params, region, type Sp } from "../lib/api";
import { Empty, Loading, Pager, RegionTabs } from "../lib/ui";

type Item = { id: string; name: string; website_url: string | null; emerging_global: number | null; emerging_india: number | null; india: boolean; rounds: number; last_round_at: string | null };

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const r = region(p);
  const d = await get<{ total: number; items: Item[] }>("/api/companies", { q: p.q, region: r, sort: p.sort, offset: p.offset });
  return (
    <>
      <RegionTabs path="/companies" region={r} extra={{ q: p.q, sort: p.sort }} />
      <form className={styles.filters}>
        <input name="q" placeholder="Company name" defaultValue={p.q} />
        <input type="hidden" name="region" value={r} />
        <select name="sort" defaultValue={p.sort ?? "emerging"}>
          <option value="emerging">Emerging score</option><option value="recent">Newest</option><option value="name">Name</option>
        </select>
        <button>Apply</button>
      </form>
      {d.items.length ? (
        <div className={styles.scroll}>
          <table className={styles.table}><thead><tr><th>Company</th><th>Emerging (global)</th><th>Emerging (India)</th><th>Rounds</th><th>Last round</th><th>India</th></tr></thead>
            <tbody>{d.items.map((c) => (
              <tr key={c.id}><td><Link href={`/companies/${c.id}`}>{c.name}</Link></td><td>{c.emerging_global ?? "—"}</td><td>{c.emerging_india ?? "—"}</td>
                <td>{c.rounds}</td><td>{day(c.last_round_at)}</td><td>{c.india ? "yes" : "no"}</td></tr>
            ))}</tbody></table>
        </div>
      ) : <Empty what="companies" />}
      <Pager path="/companies" p={{ ...p, region: r }} total={d.total} />
    </>
  );
}

export default function Companies({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Companies</h1>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
