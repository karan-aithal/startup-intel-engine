"use client";
import { useEffect, useSyncExternalStore } from "react";

// "New since your last visit": the last visit time lives in the browser (localStorage). The previous visit is fixed for the tab session
// (sessionStorage), so paging and filtering keep the same marks instead of clearing them on the first click.
const read = (k: string, s: Storage) => { try { return s.getItem(k); } catch { return null; } };
const write = (k: string, v: string, s: Storage) => { try { s.setItem(k, v); } catch { /* private mode: no marker */ } };

function since(): number {
  let p = read("explore:prev", sessionStorage);
  if (p === null) { p = read("explore:last", localStorage) ?? "0"; write("explore:prev", p, sessionStorage); }
  return Number(p);
}
const none = () => () => {};

export function Visit() {
  useEffect(() => { since(); write("explore:last", String(Date.now()), localStorage); }, []);  // since() first: it fixes the previous visit before this one overwrites it
  return null;
}

export function New({ at }: { at: string | null }) {
  const prev = useSyncExternalStore(none, since, () => 0);  // the server and first paint mark nothing
  // first visit ever (prev 0) marks nothing: everything would be "new"
  return prev && at && new Date(at).getTime() > prev ? <span title="Detected since your last visit"> ●</span> : null;
}
