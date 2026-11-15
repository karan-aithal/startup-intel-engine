"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import styles from "./checklist.module.scss";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const KEY = "alert-checklist-done";

type Checklist = {
  portals: { name: string; alerts: string; also?: string }[];
  newsletters: { name: string; kind?: string }[];
  google_alerts: { funding_phrases: string[]; hiring_phrases: string[]; investors: string[]; companies: string[] };
};

export default function ChecklistPage() {
  const [data, setData] = useState<Checklist | null>(null);
  // Checkboxes only render after the fetch below, so reading storage here cannot cause a hydration mismatch.
  const [done, setDone] = useState<Record<string, boolean>>(() => {
    try {
      return typeof window === "undefined" ? {} : JSON.parse(localStorage.getItem(KEY) ?? "{}");
    } catch {
      return {}; // ticks are a convenience; start empty if storage is unavailable
    }
  });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/alert-checklist`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setError("Backend unreachable"));
  }, []);

  // ponytail: ticks live in this browser's localStorage; move to a DB table if you use several devices.
  const toggle = (id: string) => {
    const next = { ...done, [id]: !done[id] };
    setDone(next);
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  };

  const row = (id: string, label: string, note?: string) => (
    <label key={id} className={styles.row}>
      <input type="checkbox" checked={!!done[id]} onChange={() => toggle(id)} />
      <span>
        {label}
        {note && <small>{note}</small>}
      </span>
    </label>
  );

  const g = data?.google_alerts;
  return (
    <main className={styles.main}>
      <Link href="/">← Home</Link>
      <h1>Alert checklist</h1>
      <p>
        Set these up by hand, all delivering to the dedicated alerts inbox. After each one, forward one real email to
        the inbox so its parser can be written.
      </p>
      {error && <p className={styles.bad}>{error}</p>}
      {data && (
        <>
          <h2>Job portals</h2>
          {data.portals.map((p) => row(`portal:${p.name}`, p.name, [p.alerts, p.also].filter(Boolean).join(" · ")))}
          <h2>Newsletters</h2>
          {data.newsletters.map((n) => row(`news:${n.name}`, n.name, n.kind))}
          <h2>Google Alerts: funding phrases</h2>
          {g?.funding_phrases.map((x) => row(`gf:${x}`, x))}
          <h2>Google Alerts: hiring phrases</h2>
          {g?.hiring_phrases.map((x) => row(`gh:${x}`, x))}
          <h2>Google Alerts: investors</h2>
          {g?.investors.map((x) => row(`gi:${x}`, x))}
          <h2>Google Alerts: companies</h2>
          {g?.companies.map((x) => row(`gc:${x}`, x))}
        </>
      )}
    </main>
  );
}
