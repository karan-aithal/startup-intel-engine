import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { day, get } from "../lib/api";
import { Loading, Pill } from "../lib/ui";

type Data = {
  candidates: { id: string; name: string; domain: string | null; status: string; match_confidence: number | null; matched_company_id: string | null; matched_company: string | null; created_at: string }[];
  candidate_status: { status: string; n: number }[];
  boards: { provider: string; board_token: string; status: string; discovered_via: string | null; last_job_count: number | null; company_id: string; company: string; created_at: string }[];
  board_status: { status: string; n: number }[];
  company_sources: { kind: string; status: string; n: number }[];
};

const Counts = ({ rows }: { rows: { status: string; n: number }[] }) => (
  <p>{rows.map((r) => <span key={r.status} style={{ marginRight: "1rem" }}><Pill v={r.status} /> {r.n}</span>)}</p>
);

async function Content() {
  const d = await get<Data>("/api/discovery");
  return (
    <>
      <h2>Company candidates</h2>
      <Counts rows={d.candidate_status} />
      <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Name</th><th>Domain</th><th>Status</th><th>Match</th><th>Found</th></tr></thead>
        <tbody>{d.candidates.map((c) => (
          <tr key={c.id}><td>{c.name}</td><td>{c.domain ?? "—"}</td><td><Pill v={c.status} /></td>
            <td>{c.matched_company_id ? <><Link href={`/companies/${c.matched_company_id}`}>{c.matched_company}</Link> ({c.match_confidence ?? "—"})</> : "—"}</td><td>{day(c.created_at)}</td></tr>
        ))}</tbody></table></div>
      <h2>ATS boards</h2>
      <Counts rows={d.board_status} />
      <div className={styles.scroll}><table className={styles.table}><thead><tr><th>Company</th><th>Provider</th><th>Token</th><th>Status</th><th>Via</th><th>Jobs</th><th>Found</th></tr></thead>
        <tbody>{d.boards.map((b) => (
          <tr key={b.provider + b.board_token}><td><Link href={`/companies/${b.company_id}`}>{b.company}</Link></td><td>{b.provider}</td><td>{b.board_token}</td>
            <td><Pill v={b.status} /></td><td>{b.discovered_via ?? "—"}</td><td>{b.last_job_count ?? "—"}</td><td>{day(b.created_at)}</td></tr>
        ))}</tbody></table></div>
      <h2>Company sources</h2>
      {d.company_sources.length ? (
        <div className={styles.scroll}><table className={styles.table}><tbody>{d.company_sources.map((s) => <tr key={s.kind + s.status}><td>{s.kind}</td><td><Pill v={s.status} /></td><td>{s.n}</td></tr>)}</tbody></table></div>
      ) : <p className={styles.muted}>None registered.</p>}
    </>
  );
}

export default function Discovery() {
  return (
    <main className={styles.page}>
      <h1>Discovery</h1>
      <Suspense fallback={<Loading />}><Content /></Suspense>
    </main>
  );
}
