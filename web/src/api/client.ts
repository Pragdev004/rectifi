import { useSyncExternalStore } from "react";

/** The server answered with an error. `message` is one plain sentence that is safe to show. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly field: string | null;
  constructor(status: number, code: string, message: string, field: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

/** The server did not answer at all (laptop asleep, app stopped, no network). */
export class NetworkError extends Error {
  constructor() {
    super("Rectifi on your laptop isn't answering.");
    this.name = "NetworkError";
  }
}

// ── connection state: the shell shows a banner while the laptop isn't answering ──

let offline = false;
const listeners = new Set<() => void>();
function setOffline(value: boolean) {
  if (offline === value) return;
  offline = value;
  listeners.forEach((l) => l());
}
export function useOffline(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => offline,
    () => false,
  );
}

export const REQUEST_HEADER = "X-Rectifi-Request";

export interface RequestOptions {
  body?: unknown;
  form?: FormData;
  signal?: AbortSignal;
}

async function toError(res: Response): Promise<ApiError> {
  try {
    const data = (await res.json()) as { error?: { code?: string; message?: string; field?: string | null } };
    if (data.error?.message) return new ApiError(res.status, data.error.code ?? "error", data.error.message, data.error.field ?? null);
  } catch {
    /* not JSON */
  }
  return new ApiError(res.status, "error", "Something went wrong. Try again.");
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { [REQUEST_HEADER]: "1", Accept: "application/json" };
  let body: BodyInit | undefined;
  if (options.form) {
    body = options.form; // the browser adds the multipart boundary
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  } else if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    body = "{}";
  }
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { method, headers, body, signal: options.signal, cache: "no-store", credentials: "same-origin" });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    setOffline(true);
    throw new NetworkError();
  }
  setOffline(false);
  if (!res.ok) {
    const err = await toError(res);
    if (err.code === "auth_required") window.dispatchEvent(new Event("rectifi:auth-required")); // api/auth.ts sends the person to /login
    throw err;
  }
  return (await res.json()) as T;
}

/** A file the server sends back (the account export ZIP). Same headers and error shape as `request`; the answer is a Blob and the file name it suggests. */
export async function requestBlob(path: string, signal?: AbortSignal): Promise<{ blob: Blob; filename: string | null }> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { method: "GET", headers: { [REQUEST_HEADER]: "1" }, signal, cache: "no-store", credentials: "same-origin" });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    setOffline(true);
    throw new NetworkError();
  }
  setOffline(false);
  if (!res.ok) {
    const err = await toError(res);
    if (err.code === "auth_required") window.dispatchEvent(new Event("rectifi:auth-required"));
    throw err;
  }
  const named = /filename="?([^";]+)"?/i.exec(res.headers.get("Content-Disposition") ?? "");
  return { blob: await res.blob(), filename: named?.[1] ?? null };
}

export const get = <T,>(path: string, signal?: AbortSignal) => request<T>("GET", path, { signal });
export const post = <T,>(path: string, body?: unknown) => request<T>("POST", path, { body });
export const put = <T,>(path: string, body?: unknown) => request<T>("PUT", path, { body });
export const patch = <T,>(path: string, body?: unknown) => request<T>("PATCH", path, { body });
export const del = <T,>(path: string) => request<T>("DELETE", path);
/** DELETE with a JSON body (deleting an account needs the password and the typed username). */
export const delWith = <T,>(path: string, body: unknown) => request<T>("DELETE", path, { body });

/** Query-string helper that leaves out empty values. */
export function qs(params: Record<string, string | number | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "") u.set(k, String(v));
  }
  const text = u.toString();
  return text ? `?${text}` : "";
}

export function isDbDown(error: unknown): boolean {
  return error instanceof ApiError && error.code === "db_unavailable";
}
