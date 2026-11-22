import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { get, when } from "../lib/api";
import { Loading, Pill } from "../lib/ui";
import { AddPortal, RowActions } from "./actions";

type Board = {
  route_id: string | null; name: string; kind: string; state: string; enabled: boolean; provider?: string; token?: string; board_url?: string | null;
  company_id?: string | null; company?: string | null; discovered_via?: string | null; small_board?: boolean; last_job_count?: number | null;
  open: number; india: number; remote: number; last_success_at: string | null; failures: number; last_error: string | null; events_24h?: number;
};
type Data = {
  states: Record<string, number>;
  company_boards: Board[];
  job_boards: Board[];
  no_ats: { company_id: string; company: string; website_url: string | null; careers_url: string | null }[];
  caps: { companies: { used: number; cap: number }; boards: { used: number; cap: number } };
};

const ORDER = ["live", "empty", "broken", "pending", "candidate", "disabled", "no_ats"];

async function Content() {
  const d = await get<Data>("/api/portals");
  return (
    <>
      <p>
        {ORDER.filter((s) => d.states[s]).map((s) => <span key={s} style={{ marginRight: "1rem" }}><Pill v={s} /> {d.states[s]}</span>)}
      </p>
      <p className={styles.muted}>
        Watched companies {d.caps.companies.used} of {d.caps.companies.cap} · Active company boards {d.caps.boards.used} of {d.caps.boards.cap}. New boards respect both caps.
      </p>
      <h2>Add a portal</h2>
      <p className={styles.muted}>Pick a company already in the database, then paste its careers page or a board URL. The board is verified (it must list jobs) before it is stored.</p>
      <AddPortal />

      <h2>Company boards</h2>
      <div className={styles.scroll}>
        <table className={styles.table} aria-label="Company boards">
          <thead><tr><th>Company / board</th><th>State</th><th>Open</th><th>India</th><th>Remote</th><th>Listed last poll</th><th>Last success</th><th>Found via</th><th>Actions</th></tr></thead>
          <tbody>{d.company_boards.map((b) => (
            <tr key={b.name}>
              <td>
                {b.company_id ? <Link href={`/companies/${b.company_id}`}>{b.company}</Link> : <span>{b.token} <span className={styles.muted}>(seed list)</span></span>}
                <br /><span className={styles.muted}>{b.board_url ? <a href={b.board_url} rel="noopener noreferrer">{b.name}</a> : b.name}</span>
              </td>
              <td><Pill v={b.state} />{b.small_board && <> <Pill v="small board" /></>}{b.state === "broken" && b.last_error && <><br /><span className={styles.muted}>{b.last_error.slice(0, 80)}</span></>}</td>
              <td>{b.open}</td><td>{b.india}</td><td>{b.remote}</td><td>{b.last_job_count ?? "—"}</td><td>{when(b.last_success_at)}</td><td>{b.discovered_via ?? "—"}</td>
              <td><RowActions routeId={b.route_id} enabled={b.enabled} state={b.state} name={b.name} /></td>
            </tr>
          ))}</tbody>
        </table>
      </div>

      <h2>Job boards</h2>
      <p className={styles.muted}>Board-wide sources: only jobs open to India are stored. Jooble stays off until its terms and key are set in the config.</p>
      <div className={styles.scroll}>
        <table className={styles.table} aria-label="Job boards">
          <thead><tr><th>Board</th><th>State</th><th>Open</th><th>India</th><th>Remote</th><th>New in 24 h</th><th>Last success</th><th>Actions</th></tr></thead>
          <tbody>{d.job_boards.map((b) => (
            <tr key={b.name}>
              <td>{b.name}</td><td><Pill v={b.state} /></td><td>{b.open}</td><td>{b.india}</td><td>{b.remote}</td><td>{b.events_24h ?? 0}</td><td>{when(b.last_success_at)}</td>
              <td>{b.kind === "jooble" ? <span className={styles.muted}>gated</span> : <RowActions routeId={b.route_id} enabled={b.enabled} state={b.state} name={b.name} />}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>

      <h2>Watched companies with no board</h2>
      {d.no_ats.length ? (
        <div className={styles.scroll}>
          <table className={styles.table} aria-label="Watched companies with no board">
            <thead><tr><th>Company</th><th>Website</th><th>Careers page</th></tr></thead>
            <tbody>{d.no_ats.map((c) => (
              <tr key={c.company_id}>
                <td><Link href={`/companies/${c.company_id}`}>{c.company}</Link></td>
                <td>{c.website_url ? <a href={c.website_url} rel="noopener noreferrer">{c.website_url}</a> : "—"}</td>
                <td>{c.careers_url ? <a href={c.careers_url} rel="noopener noreferrer">{c.careers_url}</a> : "—"}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <p className={styles.muted}>None: every watched company has an active board.</p>}
    </>
  );
}

export default function Portals() {
  return (
    <main className={styles.page}>
      <h1>Portals</h1>
      <p className={styles.muted}>
        The companies and boards that feed the job data, and whether each one is working. Live: polling fine with jobs. Empty: polling fine, no jobs.
        Broken: circuit open, 3 failures in a row, or no success for 3 days. Company candidates and sources are on <Link href="/discovery">Discovery</Link>.
      </p>
      <Suspense fallback={<Loading />}><Content /></Suspense>
    </main>
  );
}
