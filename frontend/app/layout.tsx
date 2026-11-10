import type { Metadata } from "next";
import "./globals.scss";

export const metadata: Metadata = {
  title: "Startup Intelligence Platform",
  description: "Funding, hiring and emerging startup intelligence",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
