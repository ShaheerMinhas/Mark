import { writeFileSync, mkdirSync } from "fs";

/** Netlify build: proxy /api/* to Railway when VITE_API_URL is set. */
const api = (process.env.VITE_API_URL || process.env.RAILWAY_PUBLIC_URL || "").replace(/\/$/, "");

let redirects = "/*    /index.html   200\n";
if (api) {
  redirects = `/api/*  ${api}/api/:splat  200\n` + redirects;
  console.log(`API proxy -> ${api}`);
} else {
  console.warn("VITE_API_URL not set — set it in Netlify env vars to your Railway URL");
}

mkdirSync("public", { recursive: true });
writeFileSync("public/_redirects", redirects);
