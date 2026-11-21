"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "../ui.module.scss";

// The browser calls the API directly, as /watchlists does: the write guard checks the Origin header, which only a browser page on an allowed origin sends.
const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Reply = { detail?: unknown; results?: { provider: string; token: string; result: string }[]; companies?: Hit[] } | null;

async function post(path: string, body: unknown): Promise<{ ok: boolean; data: Reply }> {
  const r = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return { ok: r.ok, data: await r.json().catch(() => null) };
}

const why = (d: Reply) => (typeof d?.detail === "string" ? d.detail : "Request failed");

export function RowActions({ routeId, enabled, state }: { routeId: string | null; enabled: boolean; state: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  if (!routeId) return <span className={styles.muted}>—</span>;
  const run = async (path: string, body: unknown, done: string) => {
    setBusy(true);
    setNote(null);
    const { ok, data } = await post(path, body);
    setNote(ok ? done : why(data));
    setBusy(false);
    if (ok) router.refresh();
  };
  return (
    <span>
      <button type="button" disabled={busy} onClick={() => run(`/api/portals/${routeId}/enabled`, { enabled: !enabled }, enabled ? "Disabled" : "Enabled")}>
        {enabled ? "Disable" : "Enable"}
      </button>{" "}
      {enabled && state !== "candidate" && (
        <button type="button" disabled={busy} onClick={() => run(`/api/portals/${routeId}/recheck`, {}, "Re-check queued; the worker polls it within seconds")}>Re-check</button>
      )}
      {note && <span className={styles.muted}> {note}</span>}
    </span>
  );
}

type Hit = { id: string; name: string; website_url: string | null };

export function AddPortal() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [company, setCompany] = useState<Hit | null>(null);
  const [url, setUrl] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const find = async (v: string) => {
    setQ(v);
    setCompany(null);
    if (v.trim().length < 2) return setHits([]);
    const r = await fetch(`${API}/api/search?q=${encodeURIComponent(v.trim())}`);
    setHits(r.ok ? (((await r.json()) as Reply)?.companies ?? []).slice(0, 6) : []);
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim() || (!company && q.trim().length < 2)) return setMsg("Enter a company name and its careers URL.");
    // a company picked from the list is reused; any other name is added as a new company
    const { ok, data } = await post("/api/portals", company ? { company_id: company.id, url: url.trim() } : { company_name: q.trim(), url: url.trim() });
    if (!ok) return setMsg(why(data));
    setMsg((data?.results ?? []).map((x) => `${x.provider}:${x.token} → ${x.result}`).join("; "));
    setUrl("");
    router.refresh();
  };

  return (
    <form onSubmit={submit} className={styles.filters}>
      <input value={q} onChange={(e) => find(e.target.value)} placeholder="Company (existing, or a new name)" aria-label="Company" />
      {!company && hits.length > 0 && (
        <select aria-label="Matching companies" defaultValue="" onChange={(e) => { const h = hits.find((x) => x.id === e.target.value) ?? null; setCompany(h); if (h) setQ(h.name); }}>
          <option value="" disabled>Pick a company…</option>
          {hits.map((h) => <option key={h.id} value={h.id}>{h.name}{h.website_url ? ` (${h.website_url})` : ""}</option>)}
        </select>
      )}
      <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="Careers or board URL, e.g. https://acme.com/careers" aria-label="Careers URL" size={44} />
      <button type="submit">Add portal</button>
      {msg && <span className={styles.muted}>{msg}</span>}
    </form>
  );
}
