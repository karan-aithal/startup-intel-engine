import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { get, href, params, when, type Sp } from "../lib/api";
import { Empty, Loading, Pill } from "../lib/ui";
import { RowActions } from "./actions";

type Filters = { mode: string; q: string; exclude: string; location: string; source: string; days: number; sort: string; status: string };
type Search = {
  id: string; name: string; filters: Filters; interval_value: number; interval_unit: string; timezone: string; status: string;
  last_run_at: string | null; next_run_at: string; runs: number; runs_ok: number; last_new: number | null;
};
type Run = { id: string; search_id: string; name: string; run_no: number; started_at: string; state: string; total_matches: number; new_matches: number; error: string | null; seconds: string | null };
type Source = { id: string; name: string; health_status: string; consecutive_failures: number; last_success_at: string | null; next_poll_at: string | null; events_24h: number };
type Data = {
  searches: Search[]; runs: Run[]; schedule: Source[]; worker_last_seen: string | null; worker_up: boolean;
  limits: { max: number; min_interval_minutes: number; cooldown_seconds: number };
  cards: { active: number; paused: number; runs_24h: number; runs_7d: number; ok_7d: number };
};

const summary = (f: Filters) =>
  [f.mode !== "all" && f.mode, f.q && `roles: ${f.q}`, f.exclude && `not: ${f.exclude}`, f.location && `in: ${f.location}`, f.source && `from: ${f.source}`, `${f.days}d`, f.status === "all" && "incl. closed"]
    .filter(Boolean).join(" · ");
const every = (s: Search) => `every ${s.interval_value} ${s.interval_unit.replace(/s$/, s.interval_value === 1 ? "" : "s")}`;
const rate = (ok: number, n: number) => (n ? `${Math.round((100 * ok) / n)}%` : "—");

async function Content({ sp }: { sp: Sp }) {
  const p = await params(sp);
  const d = await get<Data>("/api/scans", { search_id: p.search });
  return (
    <>
      <div className={styles.grid}>
        <div className={styles.stat}><b>{d.cards.active}</b><span>Active searches{d.cards.paused ? ` (${d.cards.paused} paused)` : ""}</span></div>
        <div className={styles.stat}><b>{d.cards.runs_24h}</b><span>Runs in 24 h</span></div>
        <div className={styles.stat}><b>{rate(d.cards.ok_7d, d.cards.runs_7d)}</b><span>Run success, 7 days</span></div>
        <div className={styles.stat}><b>{d.worker_up ? "Up" : "Stale"}</b><span>Worker, seen {when(d.worker_last_seen)}</span></div>
      </div>

      <h2>Saved searches</h2>
      <p className={styles.muted}>
        Save one from <Link href="/explore">Explore</Link> with its Schedule button. Up to {d.limits.max} searches, at least {d.limits.min_interval_minutes} minutes apart.
        A search reports only jobs detected after it was saved (or after its last run); &ldquo;Run now&rdquo; waits {d.limits.cooldown_seconds} s between runs.
      </p>
      {d.searches.length ? (
        <div className={styles.scroll}>
          <table className={styles.table} aria-label="Saved searches">
            <thead><tr><th>Search</th><th>Filters</th><th>Schedule</th><th>Status</th><th>Last run</th><th>Next run</th><th>Runs</th><th>New (last)</th><th>Actions</th></tr></thead>
            <tbody>{d.searches.map((s) => (
              <tr key={s.id}>
                <td><Link href={href("/explore", { ...s.filters })}>{s.name}</Link><br /><Link className={styles.muted} href={href("/scans", { search: s.id })}>history</Link></td>
                <td>{summary(s.filters)}</td>
                <td>{every(s)}<br /><span className={styles.muted}>{s.timezone}</span></td>
                <td><Pill v={s.status} /></td>
                <td>{when(s.last_run_at)}</td>
                <td>{s.status === "active" ? when(s.next_run_at) : "—"}</td>
                <td>{s.runs} ({rate(s.runs_ok, s.runs)} ok)</td>
                <td>{s.last_new ?? "—"}</td>
                <td><RowActions id={s.id} status={s.status} name={s.name} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <Empty what="saved searches" />}

      <h2>Run history{p.search ? <> · <Link href="/scans">all searches</Link></> : ""}</h2>
      {d.runs.length ? (
        <div className={styles.scroll}>
          <table className={styles.table} aria-label="Run history">
            <thead><tr><th>Search</th><th>Started</th><th>State</th><th>Took</th><th>Matches</th><th>New</th><th>Error</th></tr></thead>
            <tbody>{d.runs.map((r) => (
              <tr key={r.id}>
                <td>{r.name} <span className={styles.muted}>#{r.run_no}</span></td><td>{when(r.started_at)}</td><td><Pill v={r.state} /></td>
                <td>{r.seconds ? `${r.seconds} s` : "—"}</td><td>{r.total_matches}</td><td>{r.new_matches}</td><td>{r.error ?? ""}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <Empty what="runs" />}

      <h2>Collection schedule</h2>
      <p className={styles.muted}>
        Collection runs on its own, all day: the worker polls each source below when it is due. Scans only search what was collected.
        Details and failures are on <Link href="/sources">Source Health</Link>.
      </p>
      <div className={styles.scroll}>
        <table className={styles.table} aria-label="Collection schedule">
          <thead><tr><th>Source</th><th>Health</th><th>Last success</th><th>Next poll</th><th>New in 24 h</th></tr></thead>
          <tbody>{d.schedule.map((s) => (
            <tr key={s.id}><td>{s.name}</td><td><Pill v={s.health_status} /></td><td>{when(s.last_success_at)}</td><td>{when(s.next_poll_at)}</td><td>{s.events_24h}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}

export default function Scans({ searchParams }: { searchParams: Sp }) {
  return (
    <main className={styles.page}>
      <h1>Scheduled scans</h1>
      <p className={styles.muted}>Explore searches the worker re-runs on a schedule. Each run sends one digest of the new matches (topic &ldquo;Job Scans&rdquo;; the console until Telegram is set).</p>
      <Suspense fallback={<Loading />}><Content sp={searchParams} /></Suspense>
    </main>
  );
}
