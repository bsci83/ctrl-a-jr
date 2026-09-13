"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { Badge, Mono, cn } from "./primitives";

const LINKS = [
  { href: "/", label: "Workspace" },
  { href: "/channels", label: "Channels" },
  { href: "/verdict", label: "Verdict" },
];

/**
 * The selected run lives in the URL (`?run=`), so it survives navigation
 * between the three views and a shared link opens on the same run. The nav
 * carries it across.
 */
function NavLinks() {
  const pathname = usePathname();
  const params = useSearchParams();
  const run = params.get("run");
  const qs = run ? `?run=${encodeURIComponent(run)}` : "";

  return (
    <nav className="flex items-center gap-1" aria-label="Views">
      {LINKS.map((l) => {
        const active = l.href === "/" ? pathname === "/" : pathname.startsWith(l.href);
        return (
          <Link
            key={l.href}
            href={`${l.href}${qs}`}
            aria-current={active ? "page" : undefined}
            className={cn(
              "relative rounded-lg px-3 py-1.5 text-xs font-bold tracking-wide transition-colors",
              active
                ? "text-[var(--on-surface)]"
                : "text-[var(--on-surface-variant)] hover:bg-[var(--surface-high)] hover:text-[var(--on-surface)]",
            )}
          >
            {l.label}
            {active ? (
              <span
                aria-hidden
                className="absolute right-2 -bottom-[9px] left-2 h-[2px] rounded-full"
                style={{ background: "var(--primary)" }}
              />
            ) : null}
          </Link>
        );
      })}
    </nav>
  );
}

export function TopNav({
  commit,
  generatedAt,
}: {
  commit: string | null;
  generatedAt: string | null;
}) {
  return (
    <header className="sticky top-0 z-20 border-b border-[var(--outline)] bg-[var(--surface)]/95 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[1600px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-3 sm:px-6">
        <Link href="/" className="flex min-w-0 items-center gap-2.5">
          <span
            aria-hidden
            className="flex size-7 shrink-0 items-center justify-center rounded-lg text-[0.625rem] font-black text-white"
            style={{
              background: "linear-gradient(135deg, var(--primary), var(--primary-container))",
            }}
          >
            JR
          </span>
          <span className="min-w-0">
            <span className="block text-sm leading-none font-black tracking-tight">
              ctrl-a <span className="text-[var(--primary-bright)]">JR</span>
            </span>
            <span className="block text-[0.625rem] leading-none text-[var(--on-surface-muted)]">
              detailing shop · evidence
            </span>
          </span>
        </Link>

        <Suspense fallback={<div className="h-[30px]" />}>
          <NavLinks />
        </Suspense>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Badge tone="info" title="Nothing on this page can approve, deny, send or change anything. The operable approval surface is the local approval server.">
            read-only
          </Badge>
          {commit ? (
            <Mono className="text-[var(--on-surface-muted)]" >
              {commit.slice(0, 7)}
            </Mono>
          ) : null}
          {generatedAt ? (
            <span className="hidden text-[0.6875rem] text-[var(--on-surface-muted)] sm:inline">
              exported {generatedAt}
            </span>
          ) : null}
        </div>
      </div>
    </header>
  );
}
