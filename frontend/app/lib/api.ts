// Server-side helpers for the dashboard pages. Every fetch is uncached: the data changes every minute.
const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type Region = "all" | "india" | "global";
export type Sp = Promise<Record<string, string | string[] | undefined>>;

export async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]));
  const r = await fetch(`${API}${path}${qs.size ? `?${qs}` : ""}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path} returned ${r.status}`);
  return r.json();
}

export async function params(sp: Sp): Promise<Record<string, string>> {
  const raw = await sp;
  return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, Array.isArray(v) ? v[0] : (v ?? "")]));
}

export const region = (p: Record<string, string>): Region => (p.region === "india" || p.region === "global" ? p.region : "all");

export const day = (iso?: string | null) => (iso ? iso.slice(0, 10) : "—");
export const when = (iso?: string | null) => (iso ? iso.slice(0, 16).replace("T", " ") + "Z" : "—");

export function money(usd?: string | null, orig?: string | null, cur?: string | null) {
  const f = (n: number) => (n >= 1e9 ? `${+(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${+(n / 1e6).toFixed(2)}M` : n >= 1e3 ? `${+(n / 1e3).toFixed(1)}K` : String(n));
  if (usd) return `$${f(Number(usd))}`;
  if (orig) return `${f(Number(orig))} ${cur ?? ""}`.trim();
  return "—";
}

export const href = (path: string, p: Record<string, string | number | undefined>) => {
  const qs = new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== "" && v !== "all").map(([k, v]) => [k, String(v)]));
  return qs.size ? `${path}?${qs}` : path;
};
