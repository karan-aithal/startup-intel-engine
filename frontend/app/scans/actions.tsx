"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "../ui.module.scss";

// The browser calls the API directly, as /portals does: the write guard checks the Origin header, which only a browser page on an allowed origin sends.
const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export async function send(method: "POST" | "PATCH" | "DELETE", path: string, body: unknown = {}): Promise<{ ok: boolean; data: { detail?: unknown; id?: string; tracked?: number; changed?: number } | null }> {
  const r = await fetch(`${API}${path}`, { method, headers: { "Content-Type": "application/json" }, body: method === "DELETE" ? undefined : JSON.stringify(body) });
  return { ok: r.ok, data: await r.json().catch(() => null) };
}
export const why = (d: { detail?: unknown } | null) => (typeof d?.detail === "string" ? d.detail : "Request failed");

export function RowActions({ id, status, name }: { id: string; status: string; name: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [sure, setSure] = useState(false);
  const run = async (method: "POST" | "PATCH" | "DELETE", path: string, done: string) => {
    setBusy(true);
    setNote(null);
    const { ok, data } = await send(method, path);
    setNote(ok ? done : why(data));
    setBusy(false);
    setSure(false);
    if (ok) router.refresh();
  };
  const base = `/api/scans/${id}`;
  return (
    <span>
      {status === "active"
        ? <button type="button" disabled={busy} aria-label={`Pause ${name}`} onClick={() => run("POST", `${base}/pause`, "Paused")}>Pause</button>
        : <button type="button" disabled={busy} aria-label={`Resume ${name}`} onClick={() => run("POST", `${base}/resume`, "Resumed; runs within 30 s")}>Resume</button>}{" "}
      {status === "active" && <button type="button" disabled={busy} aria-label={`Run ${name} now`} onClick={() => run("POST", `${base}/run`, "Queued; the worker runs it within 30 s")}>Run now</button>}{" "}
      {sure
        ? <button type="button" disabled={busy} aria-label={`Confirm delete ${name}`} onClick={() => run("DELETE", base, "Deleted")}>Confirm delete</button>
        : <button type="button" disabled={busy} aria-label={`Delete ${name}`} onClick={() => setSure(true)}>Delete</button>}
      <span role="status" className={styles.muted}> {note}</span>
    </span>
  );
}
