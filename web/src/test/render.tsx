import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactElement } from "react";
import { Router } from "wouter";
import { authKeys, type AuthStatus } from "../api/auth";

export const LAPTOP: AuthStatus = { required: false, signed_in: true, multiuser: false, username: null, is_owner: true };
export const OWNER: AuthStatus = { required: true, signed_in: true, multiuser: true, username: "prag", is_owner: true };
export const FRIEND: AuthStatus = { required: true, signed_in: true, multiuser: true, username: "asha", is_owner: false };
export const SIGNED_OUT_MULTI: AuthStatus = { required: true, signed_in: false, multiuser: true, username: null, is_owner: false };

/** Render a page to HTML with a known sign-in status already in the cache (no network in these tests). */
export function renderWith(node: ReactElement, status: AuthStatus | null, extra?: (client: QueryClient) => void, path = "/"): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (status) client.setQueryData(authKeys.status, status);
  extra?.(client);
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <Router ssrPath={path}>{node}</Router>
    </QueryClientProvider>,
  );
}
