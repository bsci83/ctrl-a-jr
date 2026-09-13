import type { Metadata } from "next";

import { TopNav } from "@/components/top-nav";
import { getEvidence } from "@/lib/evidence";
import { stamp } from "@/lib/derive";
import "./globals.css";

export const metadata: Metadata = {
  title: "ctrl-a JR — evidence",
  description:
    "A read-only evidence surface for a gated AI agent: what it proposed, what a human approved, and what the deterministic checks can and cannot claim.",
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Read once at the shell so the header can stamp the bundle's provenance on
  // every page. `cache()` in lib/evidence means the pages' own read is free.
  const result = await getEvidence();
  const commit = result.ok ? result.bundle.commit : null;
  const generated = result.ok ? result.bundle.generated_at : null;

  return (
    <html lang="en">
      <body className="min-h-screen bg-[var(--surface)] text-[var(--on-surface)]">
        <TopNav commit={commit} generatedAt={generated ? stamp(generated) : null} />
        <main className="mx-auto w-full max-w-[1600px] px-4 pb-16 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
