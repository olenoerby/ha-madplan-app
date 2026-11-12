// MadPlan is built as plain files and served by the app's own backend inside Home Assistant.
// Home Assistant shows it under an address of its own, /api/hassio_ingress/<token>, which is only
// known when a page is asked for. So the pages are built under a placeholder, which the backend
// replaces with that address as it serves them. See BASE_PLACEHOLDER in backend/app/main.py.
export const BASE = "/__madplan_base__";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  basePath: BASE,
  trailingSlash: true,
  poweredByHeader: false,
  images: { unoptimized: true },
  env: { NEXT_PUBLIC_BASE: BASE },
};

export default nextConfig;
