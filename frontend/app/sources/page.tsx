import { Suspense } from "react";
import styles from "../ui.module.scss";
import { get, when } from "../lib/api";
import { Loading, Pill } from "../lib/ui";

type Source = {
  id: string; name: string; connector_type: string; category: string | null; enabled: boolean; health_status: string; consecutive_failures: number;
  last_success_at: string | null; last_error: string | null; circuit_open_until: string | null; next_poll_at: string | null; events_24h: number; events_total: number;
};

async function Content() {
  const d = await get<Source[]>("/api/source-health");
  const open = (s: Source) => s.circuit_open_until && new Date(s.circuit_open_until) > new Date();
  return (
    <div className={styles.scroll}>
      <table className={styles.table}><thead><tr><th>Source</th><th>Type</th><th>Health</th><th>Failures</th><th>Last success</th><th>Next poll</th><th>Events 24h</th><th>Events total</th><th>Last error</th></tr></thead>
        <tbody>{d.map((s) => (
          <tr key={s.id} style={s.enabled ? undefined : { opacity: 0.5 }}>
            <td>{s.name}{!s.enabled && <span className={styles.muted}> (disabled)</span>}</td><td>{s.connector_type}</td>
            <td><Pill v={s.health_status} />{open(s) && <span className={styles.flag}> circuit open</span>}</td>
            <td>{s.consecutive_failures}</td><td>{when(s.last_success_at)}</td><td>{when(s.next_poll_at)}</td><td>{s.events_24h}</td><td>{s.events_total}</td>
            <td className={styles.muted}>{s.last_error ?? ""}</td></tr>
        ))}</tbody></table>
    </div>
  );
}

export default function Sources() {
  return (
    <main className={styles.page}>
      <h1>Source Health</h1>
      <Suspense fallback={<Loading />}><Content /></Suspense>
    </main>
  );
}
