"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "../ui.module.scss";
import { send, why } from "../scans/actions";

type Entry = { id: string; log: { id: string; logged_on: string; status: string; channel: string; contact: string | null; notes: string | null }[]; pinned: boolean; urgency: string; next_on: string | null };

// Logs what you did elsewhere, pins the next date, or stops reminders. Nothing here contacts anyone: the dashboard only records.
export function Tools({ e, channels, today }: { e: Entry; channels: string[]; today: string }) {
  const router = useRouter();
  const [f, setF] = useState({ channel: channels[0], contact: "", notes: "", on: today, next_on: "" });
  const [pin, setPin] = useState(e.pinned ? (e.next_on ?? "") : "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const run = async (method: "POST" | "PUT" | "DELETE", path: string, body: unknown, done: string) => {
    setBusy(true);
    setMsg(null);
    const { ok, data } = await send(method as "POST" | "PATCH" | "DELETE", path, body);
    setMsg(ok ? done : why(data));
    setBusy(false);
    if (ok) router.refresh();
  };
  const set = (k: keyof typeof f) => (ev: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: ev.target.value });
  const base = `/api/followups/${e.id}`;
  return (
    <details>
      <summary>Log / pin / stop</summary>
      <form className={styles.filters} style={{ flexDirection: "column", alignItems: "stretch", maxWidth: "24rem" }} onSubmit={(ev) => {
        ev.preventDefault();
        run("POST", `${base}/log`, { channel: f.channel, contact: f.contact, notes: f.notes, on: f.on || null, next_on: f.next_on || null }, "Logged");
      }}>
        <label>Channel <select value={f.channel} onChange={set("channel")}>{channels.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Date <input type="date" value={f.on} max={today} onChange={set("on")} /></label>
        <input value={f.contact} onChange={set("contact")} placeholder="Contact (you type it)" maxLength={300} aria-label="Contact" />
        <input value={f.notes} onChange={set("notes")} placeholder="Notes" maxLength={2000} aria-label="Notes" />
        <label>Next date (optional pin) <input type="date" value={f.next_on} min={f.on} onChange={set("next_on")} /></label>
        <button type="submit" disabled={busy}>Log follow-up</button>
      </form>
      <div className={styles.filters}>
        <label>Pin next date <input type="date" value={pin} onChange={(ev) => setPin(ev.target.value)} /></label>
        <button type="button" disabled={busy || !pin} onClick={() => run("PUT", `${base}/pin`, { on: pin }, "Pinned")}>Pin</button>
        {e.pinned && <button type="button" disabled={busy} onClick={() => run("PUT", `${base}/pin`, { on: null }, "Pin cleared")}>Clear pin</button>}
        {e.urgency === "stopped"
          ? <button type="button" disabled={busy} onClick={() => run("PUT", `${base}/stop`, { stopped: false }, "Reminders back on")}>Resume reminders</button>
          : <button type="button" disabled={busy} onClick={() => run("PUT", `${base}/stop`, { stopped: true }, "Reminders stopped")}>Stop reminders</button>}
      </div>
      {e.log.length > 0 && (
        <ul className={styles.muted}>
          {e.log.map((l) => (
            <li key={l.id}>
              {l.logged_on} · {l.channel} ({l.status}){l.contact ? ` · ${l.contact}` : ""}{l.notes ? ` · ${l.notes}` : ""}{" "}
              <button type="button" disabled={busy} onClick={() => run("DELETE", `/api/followups/log/${l.id}`, undefined, "Entry removed")}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      {msg && <p className={styles.muted}>{msg}</p>}
    </details>
  );
}
