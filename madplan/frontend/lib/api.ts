export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}

function requestHeaders(json: boolean): Record<string, string> {
  const headers: Record<string, string> = json ? { "Content-Type": "application/json" } : {};
  const language = homeAssistantLanguage();
  if (language) headers["X-MadPlan-Language"] = language.slice(0, 16);
  return headers;
}

/** Where the app is, inside Home Assistant. Filled in by the backend as it serves the files. */
export const BASE = process.env.NEXT_PUBLIC_BASE ?? "";

/**
 * The language of the person using Home Assistant, from their own profile, which can differ from
 * Home Assistant's system language. The app is shown in a frame on Home Assistant's own address, so
 * it can ask the page around it. Null outside Home Assistant.
 */
export function homeAssistantLanguage(): string | null {
  try {
    const root = window.parent?.document.querySelector("home-assistant") as { hass?: { language?: string; locale?: { language?: string } } } | null;
    const language = root?.hass?.locale?.language ?? root?.hass?.language;
    if (typeof language === "string" && language) return language;
  } catch {
    // Not inside Home Assistant's page, or another address.
  }
  try {
    // Where Home Assistant keeps a language chosen in the browser.
    const stored = window.localStorage.getItem("selectedLanguage");
    if (stored) return JSON.parse(stored) as string;
  } catch {
    // Nothing stored, or storage is blocked.
  }
  return null;
}

export async function api<T = unknown>(path: string, options: { method?: string; body?: unknown; form?: FormData } = {}): Promise<T> {
  const response = await fetch(`${BASE}/api/v1${path}`, {
    method: options.method ?? (options.body !== undefined || options.form ? "POST" : "GET"),
    credentials: "same-origin",
    headers: requestHeaders(options.body !== undefined),
    body: options.form ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
  });
  if (response.ok) {
    return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
  }
  let code = `http_${response.status}`;
  try {
    const payload = await response.json();
    if (typeof payload.detail === "string") code = payload.detail;
    else if (Array.isArray(payload.detail)) code = "validation";
  } catch {
    // Not JSON, such as the proxy answering while the backend restarts.
  }
  throw new ApiError(response.status, code);
}
