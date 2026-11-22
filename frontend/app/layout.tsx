import { Suspense } from "react";
import type { Metadata } from "next";
import Link from "next/link";
import "./globals.scss";
import styles from "./ui.module.scss";
import { get } from "./lib/api";

export const metadata: Metadata = {
  title: "Startup Intelligence Platform",
  description: "Funding, hiring and emerging startup intelligence",
};

const LINKS: [string, string][] = [
  ["/", "Overview"], ["/companies", "Companies"], ["/funding", "Funding"], ["/hiring", "Hiring"],
  ["/explore", "Explore"], ["/scans", "Scans"], ["/pipeline", "Pipeline"], ["/followups", "Follow-ups"], ["/portals", "Portals"], ["/discovery", "Discovery"], ["/sources", "Source Health"], ["/search", "Search"], ["/watchlists", "Watchlists"], ["/checklist", "Checklist"],
];

const NavLinks = ({ links }: { links: [string, string][] }) => <>{links.map(([h, t]) => <Link key={h} href={h}>{t}</Link>)}</>;

// The Follow-ups link shows only while config/followups.yaml says enabled. It streams in behind a Suspense boundary so pages can still prerender; if the
// API cannot answer, the link stays (the page explains itself).
async function Links() {
  let on = true;
  try {
    on = (await get<{ enabled: boolean }>("/api/followups/enabled")).enabled;
  } catch {}
  return <NavLinks links={on ? LINKS : LINKS.filter(([h]) => h !== "/followups")} />;
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className={styles.nav}>
          <strong>Startup Intel</strong>
          <Suspense fallback={<NavLinks links={LINKS} />}><Links /></Suspense>
        </nav>
        {children}
      </body>
    </html>
  );
}
