import { Suspense } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { get } from "../lib/api";
import { Empty, Loading, Pill } from "../lib/ui";
import { Tools } from "./actions";

type Item = {
  id: string; title: string; company_id: string | null; company_name: string | null; url: string | null; status: string; contact: string | null;
  urgency: string; next_on: string | null; days_since: number; count: number; last_on: string | null; pinned: boolean; since: string;
  log: { id: string; logged_on: string; status: string; channel: string; contact: string | null; notes: string | null }[];
};
type Data = {
  enabled: boolean; today?: string; tiles?: Record<string, number>; items?: Item[];
  settings?: { file: string; cadence: Record<string, number>; channels: string[]; reminders: { timezone: string; hour: number; quiet_from: number; quiet_until: number } };
};

const TILES = [["urgent", "Urgent"], ["overdue", "Overdue"], ["waiting", "Waiting"], ["cold", "Cold"], ["stopped", "Stopped"]] as const;
const hh = (h: number) => `${String(h).padStart(2, "0")}:00`;

async function Content() {
  const d = await get<Data>("/api/followups");
  if (!d.enabled) return <p className={styles.muted}>Follow-ups are off. Set <code>enabled: true</code> in <code>config/followups.yaml</code> to turn them back on (no restart needed).</p>;
  const { tiles, items = [], settings: s, today } = d as Required<Data>;
  return (
    <>
      <div className={styles.grid}>
        {TILES.map(([k, l]) => <div key={k} className={styles.stat}><b>{tiles[k]}</b><span>{l}</span></div>)}
      </div>
      {items.length ? (
        <div className={styles.scroll}>
          <table className={styles.table} aria-label="Follow-ups">
            <thead><tr><th>Job</th><th>Status</th><th>Verdict</th><th>Next</th><th>In status</th><th>Follow-ups</th><th>Contact</th><th>Actions</th></tr></thead>
            <tbody>{items.map((e) => (
              <tr key={e.id}>
                <td><Link href={`/pipeline/${e.id}`}>{e.title}</Link>{e.url && <> · <a href={e.url} target="_blank" rel="noreferrer">posting</a></>}<br />
                  <span className={styles.muted}>{e.company_id ? <Link href={`/companies/${e.company_id}`}>{e.company_name}</Link> : e.company_name}</span></td>
                <td><Pill v={e.status} /></td>
                <td><Pill v={e.urgency} /></td>
                <td>{e.next_on ?? "—"}{e.pinned && <span className={styles.muted}> pinned</span>}</td>
                <td>{e.days_since} d <span className={styles.muted}>since {e.since}</span></td>
                <td>{e.count}{e.last_on && <span className={styles.muted}> last {e.last_on}</span>}</td>
                <td>{e.contact ?? "—"}</td>
                <td><Tools e={e} channels={s.channels} today={today} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <Empty what="jobs to follow up on (mark a pipeline job applied, responded, assessment or interview)" />}

      <h2>Cadence</h2>
      <p className={styles.muted}>
        Read-only here; edit <code>{s.file}</code> (read on every request, no restart). Days count from the day a job entered its current status.
      </p>
      <ul>
        <li>Applied: first follow-up after {s.cadence.applied_first_days} days, then every {s.cadence.applied_repeat_days}, at most {s.cadence.applied_max_followups} (then cold; a pinned date revives it).</li>
        <li>Responded and assessment: first after {s.cadence.responded_first_days} day(s), then every {s.cadence.responded_repeat_days}.</li>
        <li>Interview: thank-you after {s.cadence.interview_thankyou_days} day(s), then every {s.cadence.responded_repeat_days}.</li>
        <li>Reminders: one per due date, queued from {hh(s.reminders.hour)}, none {hh(s.reminders.quiet_from)} to {hh(s.reminders.quiet_until)}, {s.reminders.timezone}. Channels: {s.channels.join(", ")}.</li>
      </ul>
    </>
  );
}

export default function Followups() {
  return (
    <main className={styles.page}>
      <h1>Follow-ups</h1>
      <p className={styles.muted}>When to follow up on applications, and a log of what you did. Reminders only: this page never sends email or messages and never looks up contacts.</p>
      <Suspense fallback={<Loading />}><Content /></Suspense>
    </main>
  );
}
