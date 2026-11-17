import Link from "next/link";
import styles from "../ui.module.scss";
import { href, type Region } from "./api";

export const Pill = ({ v }: { v: string | null | undefined }) => (
  <span className={`${styles.pill} ${styles[(v ?? "none").toLowerCase()] ?? ""}`}>{v ?? "—"}</span>
);

export function RegionTabs({ path, region, extra = {} }: { path: string; region: Region; extra?: Record<string, string | undefined> }) {
  return (
    <div className={styles.tabs}>
      {(["all", "india", "global"] as Region[]).map((r) => (
        <Link key={r} href={href(path, { ...extra, region: r })} className={r === region ? styles.on : ""}>
          {r === "all" ? "All" : r === "india" ? "India" : "Global"}
        </Link>
      ))}
    </div>
  );
}

export function Pager({ path, p, total, limit = 50 }: { path: string; p: Record<string, string>; total: number; limit?: number }) {
  const off = Number(p.offset) || 0;
  return (
    <div className={styles.pager}>
      <span>{total ? `${off + 1}–${Math.min(off + limit, total)} of ${total}` : "No results"}</span>
      {off > 0 && <Link href={href(path, { ...p, offset: Math.max(0, off - limit) })}>← Prev</Link>}
      {off + limit < total && <Link href={href(path, { ...p, offset: off + limit })}>Next →</Link>}
    </div>
  );
}

export const Loading = () => <p className={styles.muted}>Loading…</p>;
export const Empty = ({ what }: { what: string }) => <p className={styles.muted}>No {what} for this filter.</p>;
