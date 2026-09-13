import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * The console proxies to an agent API using a bearer token held server-side.
 * Keeping that token out of the browser is necessary and NOT sufficient: without
 * this gate the proxy authenticates on behalf of ANYONE who calls it, so a
 * stranger who finds the URL can make the agent read a real inbox, email real
 * customers and create real Stripe invoices. The token never leaks and the
 * damage is done anyway — a confused deputy.
 *
 * So the console itself needs a door. Everything except the login route and the
 * static assets requires a session cookie.
 */

const PUBLIC_PATHS = ["/login", "/api/login"];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return NextResponse.next();
  }

  const session = request.cookies.get("ctrla_console")?.value;
  if (session && session.length > 0) {
    return NextResponse.next();
  }

  // An unauthenticated API call gets JSON, not a redirect to an HTML page — a
  // fetch() that silently receives a login page renders as an unexplained parse
  // error rather than "you are signed out".
  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "not authenticated" }, { status: 401 });
  }

  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = "";
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
