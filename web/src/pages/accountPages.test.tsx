import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { type PrivacyFacts } from "../api/account";
import { FRIEND, LAPTOP, OWNER, SIGNED_OUT_MULTI, renderWith } from "../test/render";
import LoginPage from "./Login";
import PrivacyPage, { PrivacyFactsView } from "./Privacy";
import RecoverPage from "./Recover";
import SettingsPage from "./Settings";
import SignupPage from "./Signup";

const FACTS: PrivacyFacts = {
  mode: "multiuser",
  encryption_at_rest: true,
  model_provider: "local",
  model: "ollama",
  ai_for_friends: false,
  backup_days: 7,
  sealed_fields: ["transactions.payee", "transactions.raw_text", "sms_inbox.sender"],
  keyed_hash_fields: ["transactions.upi_ref", "sms_inbox.text_hash"],
  plain_fields: ["amount", "txn_date", "merchant_key (allow-listed brands only)"],
  unsealed_owner_tables: ["conversations", "trace_spans"],
  merchant_allowlist: [{ key: "swiggy", name: "Swiggy" }, { key: "dominos", name: "Domino's" }],
  who_can_read: [{ who: "Another friend, through the app", plain: "No (checks)", sealed: "No (a different key)" }],
  limits: ["There is no password reset.", "Rectifi's owner can change the code, so he could read your text if he chose to."],
  recovery: "A recovery code is the only way back. There is no email and no admin reset.",
};

describe("Sign up", () => {
  it("starts on the form, with Continue off until every box is filled", () => {
    const out = renderWith(<SignupPage />, SIGNED_OUT_MULTI);
    expect(out).toContain("Create your account");
    for (const label of ["Invite code", "Username", "Password", "Repeat password"]) expect(out).toContain(label);
    expect(/<button[^>]*type="submit"[^>]*>/.exec(out)?.[0]).toContain('aria-disabled="true"');
    expect(out).toMatch(/no password reset/i);
    expect(out).not.toContain("recovery-gate"); // the code screen only appears after the server has answered
  });
  it("says sign-up is off where there are no accounts", () => {
    expect(renderWith(<SignupPage />, LAPTOP)).toContain("Sign-up is off here");
  });
});

describe("Recover", () => {
  it("asks for the username, the code and a new password", () => {
    const out = renderWith(<RecoverPage />, SIGNED_OUT_MULTI);
    for (const label of ["Username", "Recovery code", "New password", "Repeat new password"]) expect(out).toContain(label);
    expect(out).toMatch(/There is no reset/i);
  });
});

describe("Sign in", () => {
  it("asks for a username too in multi-user mode, and links to sign up and recovery", () => {
    const out = renderWith(<LoginPage />, SIGNED_OUT_MULTI);
    expect(out).toContain("Username");
    expect(out).toContain('href="/signup"');
    expect(out).toContain('href="/recover"');
    expect(out).toContain('href="/privacy"');
  });
  it("is unchanged for one shared password: only a password box", () => {
    const out = renderWith(<LoginPage />, { required: true, signed_in: false, multiuser: false, username: null, is_owner: true });
    expect(out).not.toContain("Username");
    expect(out).toContain("dev.py set-password");
    expect(out).not.toContain('href="/signup"');
  });
});

describe("Settings", () => {
  it("shows the username and every account action to someone who has an account", () => {
    const out = renderWith(<SettingsPage />, FRIEND);
    expect(out).toContain("asha");
    for (const text of ["Change password", "New recovery code", "Export your data", "Delete my account", "Friend"]) expect(out).toContain(text);
    expect(out).toMatch(/cannot be undone, and there is no reset/i);
  });
  it("keeps the delete button off until the username is typed", () => {
    const out = renderWith(<SettingsPage />, OWNER);
    const del = /<button[^>]*>Delete my account for good<\/button>/.exec(out)?.[0] ?? "";
    expect(del).toContain('aria-disabled="true"');
    expect(out).toContain("Owner");
  });
  it("says there are no accounts on a laptop", () => {
    expect(renderWith(<SettingsPage />, LAPTOP)).toContain("There are no accounts on this Rectifi.");
  });
});

describe("Privacy", () => {
  const out = renderWith(<PrivacyFactsView facts={FACTS} />, null);
  it("carries every item the server lists", () => {
    for (const name of [...FACTS.sealed_fields, ...FACTS.keyed_hash_fields, ...FACTS.plain_fields, ...FACTS.unsealed_owner_tables]) {
      expect(out).toContain(name.replace("'", "&#x27;"));
    }
    expect(out).toContain("Swiggy");
    expect(out).toContain("Domino&#x27;s");
    expect(out).toContain("7 days");
    expect(out).toContain(FACTS.recovery);
    for (const line of FACTS.limits) expect(out).toContain(line.replace("'", "&#x27;"));
    for (const row of FACTS.who_can_read) for (const text of [row.who, row.plain, row.sealed]) expect(out).toContain(text);
  });
  it("says where the model runs and that friends' data never reaches it", () => {
    expect(out).toMatch(/on this server&#x27;s own machine/);
    expect(out).toMatch(/never sent to any model/);
  });
  it("is honest about a cloud model and about laptop mode", () => {
    const cloud = renderWith(<PrivacyFactsView facts={{ ...FACTS, model_provider: "cloud", model: "openrouter" }} />, null);
    expect(cloud).toMatch(/in the cloud/);
    const laptop = renderWith(<PrivacyFactsView facts={{ ...FACTS, mode: "laptop", encryption_at_rest: false }} />, null);
    expect(laptop).toMatch(/nothing is encrypted/i);
  });
  it("renders signed out, with a way back to sign in", () => {
    const page = renderWith(<PrivacyPage />, SIGNED_OUT_MULTI);
    expect(page).toContain("How your data is protected");
    expect(page).toContain('href="/login"');
  });
});

// A password, a recovery code or a sign-up token must never reach storage, the URL, the query cache or a log.
describe("secrets stay in component state", () => {
  const files = [
    "api/secret.ts", "api/account.ts", "components/RecoveryGate.tsx", "components/PasswordField.tsx",
    "pages/Signup.tsx", "pages/Recover.tsx", "pages/Settings.tsx", "pages/Login.tsx", "lib/recovery.ts",
  ];
  // Comments may talk about storage ("never in localStorage"); only the code is checked.
  const source = (f: string) =>
    readFileSync(new URL(`../${f}`, import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  it("uses no storage, no mutation cache, no console and no URL for them", () => {
    for (const f of files) {
      const text = source(f);
      expect(text, f).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie/);
      expect(text, f).not.toMatch(/\buseMutation\b/);
      expect(text, f).not.toMatch(/console\./);
      expect(text, f).not.toMatch(/setQueryData|setQueriesData/);
    }
  });
  it("never builds an address from a code, a password or a token", () => {
    for (const f of ["pages/Signup.tsx", "pages/Recover.tsx", "pages/Settings.tsx", "pages/Login.tsx"]) {
      expect(source(f), f).not.toMatch(/navigate\([^)]*(password|code|token|invite)/i);
    }
  });
  it("keeps the sign-up and recovery calls out of the query library", () => {
    expect(source("api/secret.ts")).not.toContain("@tanstack/react-query");
    const account = source("api/account.ts");
    expect(account.match(/useQuery/g)?.length).toBe(2); // the import and the one public Privacy read, nothing else
  });
});
