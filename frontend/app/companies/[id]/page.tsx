import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import styles from "../../ui.module.scss";
import { day, get, money, when } from "../../lib/api";
import { Loading, Pill } from "../../lib/ui";

type Round = {
  id: string; stage: string | null; amount_usd: string | null; amount_original: string | null; currency: string | null; announced_at: string | null;
  verification_status: string; confidence: number | null; is_rumour: boolean; risk_flags: Record<string, unknown>; score_global: number | null; score_india: number | null;
  india: boolean; investors: { name: string; role: string | null }[];
};
type Detail = {
  company: { id: string; name: string; website_url: string | null; careers_url: string | null; description: string | null; india: boolean; emerging_global: number | null; emerging_india: number | null };
  rounds: Round[];
  founders: { name: string; role: string | null; profile_url: string | null }[];
  ats_boards: { id: string; provider: string; board_token: string; board_url: string | null; status: string; last_job_count: number | null }[];
  sources: { kind: string; url: string | null; status: string }[];
  jobs: { id: string; title: string; location: string | null; published_at: string | null; source_url: string | null; india: boolean }[];
  jobs_total: number;
  signals: { signal_type: string; occurred_at: string }[];
  timeline: { at: string | null; kind: string; label: string; detail: string | null; url: string | null }[];
};

async function Content({ id }: { id: Promise<{ id: string }> }) {
  const { id: cid } = await id;
  let d: Detail;
  try { d = await get<Detail>(`/api/companies/${encodeURIComponent(cid)}`); } catch { notFound(); }
  const c = d.company;
  return (
    <>
      <h1>{c.name} {c.india && <span className={styles.pill}>India</span>}</h1>
      <p className={styles.muted}>
        {c.website_url && <a href={c.website_url}>Website</a>}{c.careers_url && <> · <a href={c.careers_url}>Careers</a></>}
        {" · "}Emerging: global {c.emerging_global ?? "—"}, India {c.emerging_india ?? "—"}
      </p>
      {c.description && <p>{c.description}</p>}

      <h2>Funding history</h2>
      {d.rounds.length ? (
        <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Stage</th><th>Amount</th><th>Announced</th><th>Status</th><th>Conf.</th><th>Score G / IN</th><th>Investors</th><th>Flags</th></tr></thead>
          <tbody>{d.rounds.map((r) => (
            <tr key={r.id}><td>{r.stage ?? "—"}</td><td>{money(r.amount_usd, r.amount_original, r.currency)}</td><td>{day(r.announced_at)}</td>
              <td><Pill v={r.verification_status} /></td><td>{r.confidence ?? "—"}</td><td>{r.score_global ?? "—"} / {r.score_india ?? "—"}</td>
              <td>{r.investors.map((i) => i.name).join(", ") || "—"}</td>
              <td>{Object.keys(r.risk_flags ?? {}).map((f) => <span key={f} className={styles.flag}>{f}</span>)}</td></tr>
          ))}</tbody></table></div>
      ) : <p className={styles.muted}>No funding rounds recorded.</p>}
      {d.founders.length > 0 && <p><b>Founders:</b> {d.founders.map((f) => f.name).join(", ")}</p>}

      <h2>Hiring ({d.jobs_total} jobs)</h2>
      {d.jobs.length ? (
        <>
          <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Title</th><th>Location</th><th>Published</th></tr></thead>
            <tbody>{d.jobs.map((j) => <tr key={j.id}><td>{j.source_url ? <a href={j.source_url}>{j.title}</a> : j.title}</td><td>{j.location ?? "—"}</td><td>{day(j.published_at)}</td></tr>)}</tbody></table></div>
          {d.jobs_total > d.jobs.length && <p><Link href={`/hiring?company_id=${c.id}`}>All {d.jobs_total} jobs →</Link></p>}
        </>
      ) : <p className={styles.muted}>No jobs collected. {d.ats_boards.length ? "" : "No ATS board is registered for this company."}</p>}

      <h2>Sources</h2>
      {d.ats_boards.length + d.sources.length ? (
        <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Kind</th><th>Where</th><th>Status</th></tr></thead>
          <tbody>
            {d.ats_boards.map((b) => <tr key={b.id}><td>ATS · {b.provider}</td><td>{b.board_url ? <a href={b.board_url}>{b.board_token}</a> : b.board_token}</td><td><Pill v={b.status} /></td></tr>)}
            {d.sources.map((s, i) => <tr key={i}><td>{s.kind}</td><td>{s.url ? <a href={s.url}>{s.url}</a> : "—"}</td><td><Pill v={s.status} /></td></tr>)}
          </tbody></table></div>
      ) : <p className={styles.muted}>No company sources or ATS boards registered.</p>}

      <h2>Evidence timeline</h2>
      {d.timeline.length ? (
        <ul className={styles.timeline}>{d.timeline.map((t, i) => (
          <li key={i}><time>{when(t.at)}</time><span>{t.kind}</span>
            <span>{t.url ? <a href={t.url}>{t.label}</a> : t.label}{t.detail && <span className={styles.muted}> — {t.detail}</span>}</span></li>
        ))}</ul>
      ) : <p className={styles.muted}>No evidence recorded.</p>}
    </>
  );
}

export default function Company({ params }: { params: Promise<{ id: string }> }) {
  return (
    <main className={styles.page}>
      <p><Link href="/companies">← Companies</Link></p>
      <Suspense fallback={<Loading />}><Content id={params} /></Suspense>
    </main>
  );
}
