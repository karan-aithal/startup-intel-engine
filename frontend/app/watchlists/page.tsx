"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import styles from "../checklist/checklist.module.scss";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
type Kind = "telegram" | "bluesky";
type Lists = Record<Kind, string[]>;

const SECTIONS: { kind: Kind; title: string; hint: string; placeholder: string }[] = [
  { kind: "telegram", title: "Telegram public channels", hint: "Polled every 10 min from t.me/s/<channel>. No login.", placeholder: "channel_name or t.me link" },
  { kind: "bluesky", title: "Bluesky accounts", hint: "Streamed live via Jetstream. New accounts join within 10 min.", placeholder: "name.bsky.social" },
];

export default function WatchlistsPage() {
  const [lists, setLists] = useState<Lists | null>(null);
  const [input, setInput] = useState<Record<Kind, string>>({ telegram: "", bluesky: "" });
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    fetch(`${API}/api/watchlist`)
      .then((r) => r.json())
      .then(setLists)
      .catch(() => setError("Backend unreachable"));
  }, []);
  useEffect(load, [load]);

  const call = async (kind: Kind, method: "POST" | "DELETE", value: string) => {
    setError(null);
    const r =
      method === "POST"
        ? await fetch(`${API}/api/watchlist/${kind}`, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ value }) })
        : await fetch(`${API}/api/watchlist/${kind}?value=${encodeURIComponent(value)}`, { method });
    if (!r.ok) setError((await r.json().catch(() => null))?.detail ?? "Request failed");
    else setInput((i) => ({ ...i, [kind]: "" }));
    load();
  };

  return (
    <main className={styles.main}>
      <Link href="/">← Home</Link>
      <h1>Watchlists</h1>
      <p>Edit what the pollers watch. Changes apply on the next poll; removing keeps stored history.</p>
      {error && <p className={styles.bad}>{error}</p>}
      {SECTIONS.map((s) => (
        <section key={s.kind}>
          <h2>{s.title}</h2>
          <p>{s.hint}</p>
          {lists?.[s.kind].map((v) => (
            <div key={v} className={styles.row}>
              <span>{v}</span>
              <button type="button" onClick={() => call(s.kind, "DELETE", v)}>Remove</button>
            </div>
          ))}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (input[s.kind].trim()) call(s.kind, "POST", input[s.kind]);
            }}
          >
            <input
              value={input[s.kind]}
              placeholder={s.placeholder}
              aria-label={s.title}
              onChange={(e) => setInput((i) => ({ ...i, [s.kind]: e.target.value }))}
            />{" "}
            <button type="submit">Add</button>
          </form>
        </section>
      ))}
    </main>
  );
}
