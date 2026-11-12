export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}

/** Where the app is, inside Home Assistant. Filled in by the backend as it serves the files. */
export const BASE = process.env.NEXT_PUBLIC_BASE ?? "";

export async function api<T = unknown>(path: string, options: { method?: string; body?: unknown; form?: FormData } = {}): Promise<T> {
  const response = await fetch(`${BASE}/api/v1${path}`, {
    method: options.method ?? (options.body !== undefined || options.form ? "POST" : "GET"),
    credentials: "same-origin",
    headers: options.body !== undefined ? { "Content-Type": "application/json" } : undefined,
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
