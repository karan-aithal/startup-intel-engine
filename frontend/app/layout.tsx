import type { Metadata } from "next";
import Link from "next/link";
import "./globals.scss";
import styles from "./ui.module.scss";

export const metadata: Metadata = {
  title: "Startup Intelligence Platform",
  description: "Funding, hiring and emerging startup intelligence",
};

const LINKS: [string, string][] = [
  ["/", "Overview"], ["/companies", "Companies"], ["/funding", "Funding"], ["/hiring", "Hiring"],
  ["/explore", "Explore"], ["/scans", "Scans"], ["/pipeline", "Pipeline"], ["/portals", "Portals"], ["/discovery", "Discovery"], ["/sources", "Source Health"], ["/search", "Search"], ["/watchlists", "Watchlists"], ["/checklist", "Checklist"],
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className={styles.nav}>
          <strong>Startup Intel</strong>
          {LINKS.map(([h, t]) => <Link key={h} href={h}>{t}</Link>)}
        </nav>
        {children}
      </body>
    </html>
  );
}
