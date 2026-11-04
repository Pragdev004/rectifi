import { useQuery } from "@tanstack/react-query";
import { delWith, get, post, requestBlob } from "./client";

// Every call here that carries a secret is called through `useSecretCall` (api/secret.ts), never through a mutation or a query,
// so no password, recovery code or sign-up token is ever kept in the query cache.

export interface SignupStart { signup_token: string; recovery_code: string }
export interface WhoAmI { ok: true; username: string; is_owner: boolean }

export const startSignup = (body: { invite: string; username: string; password: string }) => post<SignupStart>("/auth/signup", body);
export const confirmSignup = (signup_token: string) => post<WhoAmI>("/auth/signup/confirm", { signup_token });
export const recoverAccount = (body: { username: string; recovery_code: string; new_password: string }) =>
  post<{ recovery_code: string }>("/auth/recover", body);
export const changePassword = (body: { current: string; new: string }) => post<{ ok: true }>("/auth/change-password", body);
export const newRecoveryCode = (password: string) => post<{ recovery_code: string }>("/auth/recovery-code", { password });
export const deleteAccount = (body: { password: string; confirm_username: string }) => delWith<{ ok: true }>("/account", body);
export const exportAccount = () => requestBlob("/account/export");

// ── the public Privacy page: facts only, nothing about a person ──

export interface WhoCanRead { who: string; plain: string; sealed: string }
export interface PrivacyFacts {
  mode: "laptop" | "multiuser";
  encryption_at_rest: boolean;
  model_provider: "local" | "cloud";
  model: string;
  ai_for_friends: boolean;
  backup_days: number;
  sealed_fields: string[];
  keyed_hash_fields: string[];
  hidden_key_fields: string[];
  plain_fields: string[];
  unsealed_owner_tables: string[];
  merchant_allowlist: { key: string; name: string }[];
  who_can_read: WhoCanRead[];
  limits: string[];
  recovery: string;
}

export const usePrivacy = () =>
  useQuery({ queryKey: ["privacy"], queryFn: ({ signal }) => get<PrivacyFacts>("/privacy", signal) });
