import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import styles from "../../ui.module.scss";
import { get, money, when, day } from "../../lib/api";
import { Loading, Pill } from "../../lib/ui";
import { Edit } from "./edit";

type Detail = {
  job: {
    id: string; title: string; url: string | null; company_id: string | null; company_name: string | null; location: string | null; status: string; notes: string | null;
    contact: string | null; salary_text: string | null; next_step: string | null; applied_at: string | null; status_changed_at: string; created_at: string; last_seen_at: string | null;
  };
  history: { from_status: string | null; to_status: string; note: string | null; at: string }[];
  company: { id: string; name: string; watched: boolean } | null;
  funding: { stage: string | null; amount_usd: string | null; amount_original: string | null; currency: string | null; verification_status: string; at: string | null }[];
  closed: boolean | null;
};

async function Content({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const d = await get<Detail>(`/api/pipeline/${id}`).catch(() => null);
  if (!d) notFound();
  const { job } = d;
  return (
    <>
      <h1>{job.title}</h1>
      <p>
        {job.company_id ? <Link href={`/companies/${job.company_id}`}>{job.company_name}</Link> : (job.company_name ?? "—")} · {job.location ?? "—"} · <Pill v={job.status} />
        {d.closed === true && <> <Pill v="Closed" /></>}
      </p>
      {d.closed === true && <p className={styles.flag}>The company has taken this posting down (last seen {when(job.last_seen_at)}). Check the link before you spend time on it.</p>}
      {d.closed === null && <p className={styles.muted}>No posting record, so whether it is still open is unknown.</p>}
      {job.url && <p><a href={job.url} rel="noopener noreferrer">Open the posting</a> <span className={styles.muted}>(you apply there; this page only records it)</span></p>}
      <Edit key={job.status_changed_at} job={job} />
      <h2>History</h2>
      <ul className={styles.timeline}>
        {[...d.history].reverse().map((h, i) => (
          <li key={i}><time>{when(h.at)}</time><span>{h.from_status ? `${h.from_status} → ${h.to_status}` : `Started as ${h.to_status}`}</span><span>{h.note ?? ""}</span></li>
        ))}
      </ul>
      {d.company && (
        <>
          <h2>Company</h2>
          <p><Link href={`/companies/${d.company.id}`}>{d.company.name}</Link>{d.company.watched && <> <Pill v="Watched" /></>}</p>
          {d.funding.length ? (
            <ul>{d.funding.map((f, i) => <li key={i}>{f.stage ?? "Round"} · {money(f.amount_usd, f.amount_original, f.currency)} · {day(f.at)} · <Pill v={f.verification_status} /></li>)}</ul>
          ) : <p className={styles.muted}>No funding rounds recorded.</p>}
        </>
      )}
    </>
  );
}

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  return (
    <main className={styles.page}>
      <p><Link href="/pipeline">← Pipeline</Link></p>
      <Suspense fallback={<Loading />}><Content params={params} /></Suspense>
    </main>
  );
}
