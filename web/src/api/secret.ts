import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, NetworkError } from "./client";

/** A server call that carries a password, a code or a token.
 *
 *  It is deliberately NOT a TanStack mutation: the mutation cache keeps the variables and the answer for minutes.
 *  Here the input lives in the caller's hands, the answer is handed back once, and only a pending flag and an error stay in state.
 *  Nothing is stored in the URL, localStorage, sessionStorage or the query cache, and nothing is logged. */
export function useSecretCall<I, O>(call: (input: I) => Promise<O>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const alive = useRef(true);
  const latest = useRef(call);
  latest.current = call;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /** Resolves to the answer, or `undefined` when the call failed (the error is in `error`). */
  const run = useCallback(async (input: I): Promise<O | undefined> => {
    setPending(true);
    setError(null);
    try {
      const out = await latest.current(input);
      if (alive.current) setPending(false);
      return out;
    } catch (e) {
      if (alive.current) {
        setError(e);
        setPending(false);
      }
      return undefined;
    }
  }, []);
  const reset = useCallback(() => setError(null), []);
  return { run, pending, error, reset };
}

export interface Problem {
  code: string;
  message: string;
  field: string | null;
  locked: boolean;
}

/** The server's own sentence for a failed call, or a plain one when the server could not be reached. */
export function problemOf(error: unknown): Problem | null {
  if (error === null || error === undefined) return null;
  if (error instanceof ApiError) return { code: error.code, message: error.message, field: error.field, locked: error.code === "login_locked" };
  if (error instanceof NetworkError) {
    return { code: "network", message: "Can't reach Rectifi right now. Check your connection and try again.", field: null, locked: false };
  }
  return { code: "error", message: "Something went wrong. Try again.", field: null, locked: false };
}

/** The sentence for one field, or null when the problem belongs to another field (or none). */
export function fieldProblem(problem: Problem | null, field: string): string | null {
  return problem && problem.field === field ? problem.message : null;
}
/** The sentence to show above the form: a problem that no single field owns. */
export function formProblem(problem: Problem | null, fields: string[]): string | null {
  return problem && !(problem.field && fields.includes(problem.field)) ? problem.message : null;
}
