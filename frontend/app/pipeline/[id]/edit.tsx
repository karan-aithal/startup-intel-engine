"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "../../ui.module.scss";
import { send, why } from "../../scans/actions";

const STATES = ["saved", "applied", "responded", "assessment", "interview", "offer", "hired", "rejected", "withdrawn", "skipped"];
type Job = { id: string; status: string; notes: string | null; contact: string | null; salary_text: string | null; next_step: string | null; applied_at: string | null };

// Status move (with a history note) and the free-text fields. Only what changed is sent; "Back to Inbox" deletes the row and its history after a second click.
export function Edit({ job }: { job: Job }) {
  const router = useRouter();
  const [f, setF] = useState({ notes: job.notes ?? "", contact: job.contact ?? "", salary_text: job.salary_text ?? "", next_step: job.next_step ?? "", applied: job.applied_at?.slice(0, 10) ?? "" });
  const [status, setStatus] = useState(job.status);
  const [note, setNote] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [sure, setSure] = useState(false);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = { notes: f.notes, contact: f.contact, salary_text: f.salary_text, next_step: f.next_step };
    if (f.applied !== (job.applied_at?.slice(0, 10) ?? "")) body.applied_at = f.applied ? `${f.applied}T00:00:00Z` : null;
    if (status !== job.status) { body.status = status; body.note = note.trim() || undefined; }
    const { ok, data } = await send("PATCH", `/api/pipeline/${job.id}`, body);
    setMsg(ok ? "Saved" : why(data));
    if (ok) { setNote(""); router.refresh(); }
  };
  const restore = async () => {
    const { ok, data } = await send("DELETE", `/api/pipeline/${job.id}`);
    if (ok) router.push("/pipeline");
    else setMsg(why(data));
  };
  return (
    <form onSubmit={save} className={styles.filters} style={{ flexDirection: "column", alignItems: "stretch", maxWidth: "40rem" }}>
      <label>Status <select value={status} onChange={(e) => setStatus(e.target.value)}>{STATES.map((s) => <option key={s}>{s}</option>)}</select></label>
      {status !== job.status && <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note for the history (optional)" maxLength={500} aria-label="History note" />}
      <label>Applied on <input type="date" value={f.applied} onChange={set("applied")} /></label>
      <input value={f.next_step} onChange={set("next_step")} placeholder="Next step" maxLength={300} aria-label="Next step" />
      <input value={f.contact} onChange={set("contact")} placeholder="Contact" maxLength={300} aria-label="Contact" />
      <input value={f.salary_text} onChange={set("salary_text")} placeholder="Salary" maxLength={300} aria-label="Salary" />
      <textarea value={f.notes} onChange={set("notes")} placeholder="Notes" rows={6} maxLength={5000} aria-label="Notes" style={{ background: "var(--card)", color: "var(--fg)", border: "1px solid #2c313b", borderRadius: 6, padding: ".5rem", font: "inherit" }} />
      <span>
        <button type="submit">Save</button>{" "}
        {sure
          ? <button type="button" onClick={restore}>Confirm: delete this record and its history</button>
          : <button type="button" onClick={() => setSure(true)}>Back to Inbox</button>}
        {msg && <span className={styles.muted}> {msg}</span>}
      </span>
    </form>
  );
}
