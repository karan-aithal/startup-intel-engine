import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get, money, params, region, type Sp } from "../lib/api";
import { Empty, Loading, Pager, Pill, RegionTabs } from "../lib/ui";

type Item = {
  id: string; company_id: string; company: string; stage: string | null; amount_usd: string | null; amount_original: string | null; currency: string | null;
  announced_at: string | null; verification_status: string; confidence: number | null; is_rumour: boolean; risk_flags: Record<string, unknown>;
  score_global: number | null; score_india: number | null;
};
const STATUSES = ["VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED", "CONTRADICTED", "EXPIRED"];

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const r = region(p);
  const d = await get<{ total: number; items: Item[] }>("/api/funding", { region: r, status: p.status, stage: p.stage, sort: p.sort, offset: p.offset });
  return (
    <>
      <RegionTabs path="/funding" region={r} extra={{ status: p.status, stage: p.stage, sort: p.sort }} />
      <form className={styles.filters}>
        <input type="hidden" name="region" value={r} />
        <select name="status" defaultValue={p.status ?? ""}><option value="">Any status</option>{STATUSES.map((s) => <option key={s}>{s}</option>)}</select>
        <input name="stage" placeholder="Stage (e.g. seed)" defaultValue={p.stage} />
        <select name="sort" defaultValue={p.sort ?? "recent"}><option value="recent">Most recent</option><option value="score">Highest score</option></select>
        <button>Apply</button>
      </form>
      {d.items.length ? (
        <div className={styles.scroll}>
          <table className={styles.table}><thead><tr><th>Company</th><th>Stage</th><th>Amount</th><th>Announced</th><th>Status</th><th>Conf.</th><th>Score {r === "india" ? "(India)" : "(global)"}</th><th>Flags</th></tr></thead>
            <tbody>{d.items.map((f) => (
              <tr key={f.id}><td><Link href={`/companies/${f.company_id}`}>{f.company}</Link></td><td>{f.stage ?? "—"}</td><td>{money(f.amount_usd, f.amount_original, f.currency)}</td>
                <td>{day(f.announced_at)}</td><td><Pill v={f.verification_status} /></td><td>{f.confidence ?? "—"}</td>
                <td>{(r === "india" ? f.score_india : f.score_global) ?? "—"}</td>
                <td>{Object.keys(f.risk_flags ?? {}).map((k) => <span key={k} className={styles.flag}>{k}</span>)}</td></tr>
            ))}</tbody></table>
        </div>
      ) : <Empty what="funding rounds" />}
      <Pager path="/funding" p={{ ...p, region: r }} total={d.total} />
    </>
  );
}

export default function Funding({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Funding</h1>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
