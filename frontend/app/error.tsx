"use client"; // error boundaries must be client components

import styles from "./ui.module.scss";

// Shown inside the layout, so the nav stays usable. The thrown message is not displayed: it can contain the API path.
export default function Error({ retry }: { error: unknown; retry: () => void }) {
  return (
    <main className={styles.page}>
      <h1>Data unavailable</h1>
      <p className={styles.muted}>The dashboard could not load this page. The API may be stopped or restarting.</p>
      <div className={styles.filters}><button onClick={() => retry()}>Try again</button></div>
    </main>
  );
}
