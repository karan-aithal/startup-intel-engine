"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { send } from "../scans/actions";
import { Pill } from "../lib/ui";

// Save / Skip on an Explore row (the Pipeline). A job already tracked shows its status instead.
export function Track({ id, tracked }: { id: string; tracked: string | null }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  if (tracked) return <Pill v={tracked} />;
  const go = async (status: "saved" | "skipped") => {
    setBusy(true);
    const { ok } = await send("POST", "/api/pipeline/track", { event_ids: [id], status });
    setBusy(false);
    if (ok) router.refresh();
  };
  return <span><button type="button" disabled={busy} onClick={() => go("saved")}>Save</button> <button type="button" disabled={busy} onClick={() => go("skipped")}>Skip</button></span>;
}
