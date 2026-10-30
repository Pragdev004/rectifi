import { REQUEST_HEADER, ApiError, NetworkError } from "./client";
import type { AskEvent } from "./types";

export type EventHandler<E> = (event: E) => void;

/** POST JSON and read the reply one line at a time. Each line is one JSON event. */
export async function streamNdjson<E>(path: string, body: unknown, onEvent: EventHandler<E>, signal?: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method: "POST",
      headers: { [REQUEST_HEADER]: "1", "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(body),
      signal,
      cache: "no-store",
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new NetworkError();
  }
  if (!res.ok) {
    let message = "Something went wrong. Try again.";
    let code = "error";
    try {
      const data = (await res.json()) as { error?: { code?: string; message?: string } };
      if (data.error?.message) message = data.error.message;
      if (data.error?.code) code = data.error.code;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, code, message);
  }
  if (!res.body) throw new ApiError(500, "error", "This browser can't read a streamed answer.");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const flush = (line: string) => {
    const text = line.trim();
    if (!text) return;
    try {
      onEvent(JSON.parse(text) as E);
    } catch {
      /* a half line is ignored; the next chunk completes the next one */
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let at = buffer.indexOf("\n");
    while (at >= 0) {
      flush(buffer.slice(0, at));
      buffer = buffer.slice(at + 1);
      at = buffer.indexOf("\n");
    }
  }
  flush(buffer + decoder.decode());
}

/** The original Ask page's stream (kept for the /ask/* alias). */
export const streamAsk = (path: string, body: unknown, onEvent: EventHandler<AskEvent>, signal?: AbortSignal) =>
  streamNdjson<AskEvent>(path, body, onEvent, signal);
