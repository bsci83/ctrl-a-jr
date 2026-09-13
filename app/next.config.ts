import type { NextConfig } from "next";

// The app is a read-only evidence surface. Nothing here needs a server beyond
// rendering, and the later switch from a bundled JSON to live API polling is a
// change inside src/lib/evidence.ts alone — not a change to the routing shape.
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Pin the workspace root to this directory. Without it Turbopack walks up and
  // picks whichever lockfile it finds first — on this machine a stray
  // package-lock.json in the user's home folder, which is not this project.
  turbopack: { root: import.meta.dirname },
};

export default nextConfig;
