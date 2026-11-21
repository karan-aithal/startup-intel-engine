"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import Link from "next/link";
import styles from "../ui.module.scss";
import { send, why } from "../scans/actions";
import { Pill } from "../lib/ui";

export type Row = {
  id: string; title: string; url: string | null; company: string | null; companyId: string | null; location: string | null;
  badges: string[]; a: string; b: string; open: boolean | null; detail: boolean;
};
const STATES = ["saved", "applied", "responded", "assessment", "interview", "offer", "hired", "rejected", "withdrawn", "skipped"];
const CONFIRM = ["offer", "hired"];  // a bulk move to these asks twice: they are the ones that are painful to undo by mistake

// Rows with a checkbox and a bulk bar. In the Inbox (`inbox`) the actions are Save and Skip; on a state tab they are a status move. The browser calls the API directly (the write guard checks Origin).
export function Table({ rows, inbox, heads }: { rows: Row[]; inbox: boolean; heads: [string, string] }) {
  const router = useRouter();
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [to, setTo] = useState("applied");
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sure, setSure] = useState(false);
  const toggle = (id: string) => setSel((s) => { const n = new Set(s); if (!n.delete(id)) n.add(id); return n; });
  const all = rows.length > 0 && sel.size === rows.length;
  const go = async (ids: string[], status: string) => {
    setBusy(true);
    setMsg(null);
    const { ok, data } = inbox
      ? await send("POST", "/api/pipeline/track", { event_ids: ids, status })
      : await send("POST", "/api/pipeline/bulk", { ids, status, note: note.trim() || undefined });
    setBusy(false);
    setSure(false);
    if (!ok) return setMsg(why(data));
    setSel(new Set());
    setNote("");
    router.refresh();
  };
  const pick = [...sel];
  return (
    <>
      <div className={styles.filters}>
        {inbox ? (
          <>
            <button type="button" disabled={busy || !sel.size} onClick={() => go(pick, "saved")}>Save selected ({sel.size})</button>
            <button type="button" disabled={busy || !sel.size} onClick={() => go(pick, "skipped")}>Skip selected</button>
          </>
        ) : (
          <>
            <select value={to} onChange={(e) => { setTo(e.target.value); setSure(false); }} aria-label="Move selected to">{STATES.map((s) => <option key={s}>{s}</option>)}</select>
            <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note for the history (optional)" aria-label="History note" maxLength={500} />
            <button type="button" disabled={busy || !sel.size} onClick={() => (CONFIRM.includes(to) && !sure ? setSure(true) : go(pick, to))}>
              {sure ? `Confirm: ${sel.size} to ${to}` : `Move selected (${sel.size})`}
            </button>
          </>
        )}
        {msg && <span className={styles.muted}>{msg}</span>}
      </div>
      <div className={styles.scroll}>
        <table className={styles.table}>
          <thead><tr>
            <th><input type="checkbox" checked={all} onChange={() => setSel(all ? new Set() : new Set(rows.map((r) => r.id)))} aria-label="Select all rows on this page" /></th>
            <th>Title</th><th>Company</th><th>Location</th><th>{heads[0]}</th><th>{heads[1]}</th>{inbox && <th></th>}
          </tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.id}>
              <td><input type="checkbox" checked={sel.has(r.id)} onChange={() => toggle(r.id)} aria-label={`Select ${r.title}`} /></td>
              <td>
                {r.url ? <a href={r.url} rel="noopener noreferrer">{r.title}</a> : r.title}
                {r.detail && <> <Link className={styles.muted} href={`/pipeline/${r.id}`}>details</Link></>}
                {r.open === false && <> <Pill v="Closed" /></>}
                {r.badges.map((b) => <span key={b}> <Pill v={b} /></span>)}
              </td>
              <td>{r.companyId ? <Link href={`/companies/${r.companyId}`}>{r.company}</Link> : (r.company ?? "—")}</td>
              <td>{r.location ?? "—"}</td><td>{r.a}</td><td>{r.b}</td>
              {inbox && <td><button type="button" disabled={busy} onClick={() => go([r.id], "saved")}>Save</button> <button type="button" disabled={busy} onClick={() => go([r.id], "skipped")}>Skip</button></td>}
            </tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}
