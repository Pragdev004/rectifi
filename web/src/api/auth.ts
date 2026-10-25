import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { navigate } from "wouter/use-browser-location";
import { ApiError, NetworkError, get, post } from "./client";

/** What the server says about sign-in. On a laptop with no password, `required` is false and nothing is ever shown. */
export interface AuthStatus {
  required: boolean;
  signed_in: boolean;
}

export const authKeys = { status: ["auth", "status"] as const };

export const useAuthStatus = () =>
  useQuery({ queryKey: authKeys.status, queryFn: ({ signal }) => get<AuthStatus>("/auth/status", signal), staleTime: 30_000, retry: false });

/** Only an address inside this app is a safe place to go after signing in (no "//evil.example", no "https://..."). */
export function safeNext(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  if (raw === "/login" || raw.startsWith("/login?") || raw.startsWith("/login/")) return "/";
  if (/[\u0000-\u001f]/.test(raw)) return "/";
  return raw;
}

/** The words under the field for each way a try can fail. The server's own sentence is used when it sends one. */
export function signInProblem(error: unknown): { locked: boolean; message: string } {
  if (error instanceof ApiError) {
    return { locked: error.code === "login_locked", message: error.message };
  }
  if (error instanceof NetworkError) return { locked: false, message: "Can't reach Rectifi right now. Check your connection and try again." };
  return { locked: false, message: "Something went wrong. Try again." };
}

/** The password is sent once, in the request body, and is not kept anywhere (not in state outside the form, not in the query cache). */
export function useSignIn() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (password: string) => post<{ ok: true }>("/auth/login", { password }),
    onSuccess: () => qc.clear(), // everything fetched before sign-in was a 401; start clean
  });
}

function useLeave(path: "/auth/logout" | "/auth/logout-all") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => post<{ ok: true }>(path),
    onSuccess: () => {
      qc.clear();
      navigate("/login", { replace: true });
    },
  });
}

/** For the sign-out item in the sidebar footer and the More sheet. */
export const useSignOut = () => useLeave("/auth/logout");
/** Signs every device out (the server bumps the session epoch). */
export const useSignOutEverywhere = () => useLeave("/auth/logout-all");

// ── "the server said sign in" ──
// client.ts dispatches this event when an answer is 401 auth_required (a one-line hook, see the handoff).
// A custom event keeps client.ts and this file from importing each other.
export const AUTH_REQUIRED_EVENT = "rectifi:auth-required";

let redirectInstalled = false;
export function installAuthRedirect(): void {
  if (redirectInstalled || typeof window === "undefined") return;
  redirectInstalled = true;
  window.addEventListener(AUTH_REQUIRED_EVENT, () => {
    const here = window.location.pathname + window.location.search;
    if (window.location.pathname === "/login") return;
    navigate(here === "/" ? "/login" : `/login?next=${encodeURIComponent(here)}`, { replace: true });
  });
}
installAuthRedirect();
