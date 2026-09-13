"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function Login() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const router = useRouter();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    });
    setBusy(false);
    if (res.ok) {
      router.push("/");
      router.refresh();
    } else {
      setError(res.status === 503 ? "Console is not configured." : "Incorrect.");
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <h1 className="text-2xl font-black tracking-tight">ctrl-a JR</h1>
      <p className="mt-2 text-sm opacity-70">
        This console can make the agent send email and create invoices, so it asks who you are.
      </p>
      <form onSubmit={submit} className="mt-6 flex flex-col gap-3">
        <input
          type="password"
          value={password}
          autoFocus
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Console password"
          className="ghost-border rounded-xl px-3 py-2 text-sm"
        />
        <button
          type="submit"
          disabled={busy || password.length === 0}
          className="rounded-xl px-3 py-2 text-sm font-semibold disabled:opacity-40"
          style={{ background: "var(--accent, #111)", color: "#fff" }}
        >
          {busy ? "Checking…" : "Enter"}
        </button>
        {error ? <p className="text-sm" style={{ color: "var(--danger, #b00)" }}>{error}</p> : null}
      </form>
    </main>
  );
}
