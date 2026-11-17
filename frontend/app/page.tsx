import { Suspense } from "react";
import Link from "next/link";
import styles from "./ui.module.scss";
import { day, get, money, params, region, type Sp } from "./lib/api";
import { Empty, Loading, Pill, RegionTabs } from "./lib/ui";

type Overview = {
  stats: Record<string, number>;
  top_funding: { id: string; company_id: string; name: string; stage: string | null; amount_usd: string | null; amount_original: string | null; currency: string | null; announced_at: string | null; verification_status: string; score: number | null }[];
  top_emerging: { id: string; name: string; score: number }[];
  alerts_24h: { topic: string; n: number }[];
};

const LABELS: [string, string][] = [
  ["companies", "Companies"], ["rounds", "Funding rounds"], ["verified_rounds", "Verified rounds"], ["jobs_7d", "Jobs (7 days)"],
  ["sources_enabled", "Sources enabled"], ["sources_unhealthy", "Sources not healthy"],
];

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const r = region(p);
  const d = await get<Overview>("/api/overview", { region: r });
  return (
    <>
      <RegionTabs path="/" region={r} />
      <div className={styles.grid}>
        {LABELS.map(([k, t]) => <div key={k} className={styles.stat}><b>{d.stats[k]}</b><span>{t}</span></div>)}
      </div>
      <div className={styles.cols}>
        <section>
          <h2>Top funding rounds</h2>
          {d.top_funding.length ? (
            <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Company</th><th>Stage</th><th>Amount</th><th>Date</th><th>Status</th><th>Score</th></tr></thead>
              <tbody>{d.top_funding.map((f) => (
                <tr key={f.id}><td><Link href={`/companies/${f.company_id}`}>{f.name}</Link></td><td>{f.stage ?? "—"}</td><td>{money(f.amount_usd, f.amount_original, f.currency)}</td>
                  <td>{day(f.announced_at)}</td><td><Pill v={f.verification_status} /></td><td>{f.score ?? "—"}</td></tr>
              ))}</tbody></table></div>
          ) : <Empty what="verified or partially verified rounds" />}
        </section>
        <section>
          <h2>Emerging companies</h2>
          {d.top_emerging.length ? (
            <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Company</th><th>Score</th></tr></thead>
              <tbody>{d.top_emerging.map((c) => <tr key={c.id}><td><Link href={`/companies/${c.id}`}>{c.name}</Link></td><td>{c.score}</td></tr>)}</tbody></table></div>
          ) : <Empty what="scored companies" />}
          <h2>Alerts, last 24 hours (all regions)</h2>
          {d.alerts_24h.length ? (
            <div className={styles.scroll}><table className={styles.table}><tbody>{d.alerts_24h.map((a) => <tr key={a.topic}><td>{a.topic}</td><td>{a.n}</td></tr>)}</tbody></table></div>
          ) : <p className={styles.muted}>No alerts in the last 24 hours.</p>}
        </section>
      </div>
    </>
  );
}

export default function Overview({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Overview</h1>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
