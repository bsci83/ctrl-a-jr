import { NextResponse } from "next/server";
import { timingSafeEqual } from "node:crypto";
import process from "node:process";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * `===` on a secret leaks its prefix one byte at a time to anyone who can time
 * the response, and this endpoint is on the public internet.
 */
function matches(supplied: string, expected: string): boolean {
  const a = Buffer.from(supplied, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

export async function POST(request: Request) {
  const expected = process.env.CONSOLE_PASSWORD ?? "";
  if (!expected) {
    // A deploy that forgot the variable must refuse everyone, not admit everyone.
    return NextResponse.json({ error: "console is not configured" }, { status: 503 });
  }

  let supplied = "";
  const contentType = request.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    const body = await request.json().catch(() => ({}));
    supplied = typeof body?.password === "string" ? body.password : "";
  } else {
    const form = await request.formData().catch(() => null);
    const value = form?.get("password");
    supplied = typeof value === "string" ? value : "";
  }

  if (!matches(supplied, expected)) {
    return NextResponse.json({ error: "incorrect" }, { status: 401 });
  }

  const response = NextResponse.json({ ok: true });
  response.cookies.set("ctrla_console", "ok", {
    httpOnly: true,
    secure: true,
    sameSite: "strict",
    path: "/",
    maxAge: 60 * 60 * 12,
  });
  return response;
}
