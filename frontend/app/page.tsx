"use client";

import { useEffect, useState } from "react";
import styles from "./page.module.scss";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

type Ready = {
  status: string;
  database: { ok: boolean; server_version?: string; extensions?: string[]; migration?: string | null };
};

export default function Home() {
  const [data, setData] = useState<Ready | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // 503 still returns a JSON body describing what is degraded, so parse it either way.
    fetch(`${API}/health/ready`)
      .then((r) => r.json())
      .then(setData)
      .catch(() => setError("Backend unreachable"));
  }, []);

  return (
    <main className={styles.main}>
      <h1>Startup Intelligence Platform</h1>
      <p>Phase 0 foundation. No intelligence features yet.</p>
      <div className={styles.card}>
        <span>Backend</span>
        <span className={error ? styles.bad : data?.status === "ok" ? styles.ok : styles.bad}>
          {error ?? data?.status ?? "checking..."}
        </span>
      </div>
      {data && (
        <div className={styles.card}>
          <span>Database</span>
          <span>
            PG {data.database.server_version ?? "?"} · migration {data.database.migration ?? "none"} ·{" "}
            {data.database.extensions?.join(", ")}
          </span>
        </div>
      )}
    </main>
  );
}
