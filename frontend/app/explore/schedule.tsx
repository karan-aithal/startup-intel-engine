"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import styles from "../ui.module.scss";
import { send, why } from "../scans/actions";

// Saves the current Explore filters as a scheduled scan (the API validates them again and applies the write guard).
export function Schedule({ filters }: { filters: Record<string, string> }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [value, setValue] = useState("6");
  const [unit, setUnit] = useState("hours");
  const [msg, setMsg] = useState<string | null>(null);
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const { ok, data } = await send("POST", "/api/scans", {
      name: name.trim() || filters.q || "All jobs", filters: { ...filters, days: Number(filters.days) }, interval_value: Number(value), interval_unit: unit,
    });
    if (!ok) return setMsg(typeof data?.detail === "string" ? why(data) : "Check the interval (1 to 10000, at least 15 minutes).");
    setMsg("Saved. It reports jobs detected from now on. See Scheduled scans.");
    setOpen(false);
    router.refresh();
  };
  return (
    <form onSubmit={save} className={styles.filters}>
      {open ? (
        <>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (default: the roles)" aria-label="Search name" />
          every <input value={value} onChange={(e) => setValue(e.target.value)} size={3} inputMode="numeric" aria-label="Interval" />
          <select value={unit} onChange={(e) => setUnit(e.target.value)} aria-label="Interval unit"><option>minutes</option><option>hours</option><option>days</option></select>
          <button type="submit">Save scan</button> <button type="button" onClick={() => setOpen(false)}>Cancel</button>
        </>
      ) : <button type="button" onClick={() => { setOpen(true); setMsg(null); }}>Schedule this search</button>}
      {msg && <span className={styles.muted}>{msg}</span>}
    </form>
  );
}
