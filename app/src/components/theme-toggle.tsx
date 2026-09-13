"use client";

import { useEffect, useState } from "react";

/**
 * The stylesheet defines dark on :root and light under [data-theme="light"],
 * so switching is one attribute. The choice is persisted because a console
 * someone leaves open on a second monitor should not reset every navigation.
 *
 * The matching no-flash script lives inline in layout.tsx: this component only
 * mounts after hydration, and without that script a light-mode user gets a full
 * dark repaint on every page load.
 */
type Mode = "dark" | "light";

function apply(mode: Mode) {
  const root = document.documentElement;
  if (mode === "light") root.setAttribute("data-theme", "light");
  else root.removeAttribute("data-theme");
  try {
    localStorage.setItem("ctrla-theme", mode);
  } catch {
    // Private windows refuse storage; the toggle still works for this session.
  }
}

export function ThemeToggle() {
  const [mode, setMode] = useState<Mode>("dark");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const current =
      document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    setMode(current);
    setReady(true);
  }, []);

  function toggle() {
    const next: Mode = mode === "dark" ? "light" : "dark";
    setMode(next);
    apply(next);
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={`Switch to ${mode === "dark" ? "light" : "dark"} mode`}
      title={`Switch to ${mode === "dark" ? "light" : "dark"} mode`}
      // suppressHydrationWarning: the server cannot know the stored choice, and
      // the inline script may already have changed the label before hydration.
      suppressHydrationWarning
      className="ghost-border shrink-0 rounded-lg px-2.5 py-1.5 text-sm transition-opacity hover:opacity-80"
    >
      {ready ? (mode === "dark" ? "\u2600\uFE0F" : "\u{1F319}") : "\u25CB"}
    </button>
  );
}
