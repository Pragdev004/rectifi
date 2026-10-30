import { describe, expect, it } from "vitest";
import { safeNext, signInProblem } from "./auth";
import { ApiError, NetworkError } from "./client";

describe("safeNext keeps the person inside the app", () => {
  it("accepts an address inside the app, with its query", () => {
    expect(safeNext("/ledger")).toBe("/ledger");
    expect(safeNext("/ledger?category=Food&page=2")).toBe("/ledger?category=Food&page=2");
  });
  it("falls back to the home page for anything else", () => {
    for (const bad of [null, undefined, "", "ledger", "//evil.example", "/\\evil.example", "https://evil.example", "javascript:alert(1)", "/login", "/login?next=/ledger", "/a\nb"]) {
      expect(safeNext(bad as string | null | undefined)).toBe("/");
    }
  });
});

describe("signInProblem", () => {
  it("uses the server's sentence and marks the lock", () => {
    expect(signInProblem(new ApiError(401, "invalid_password", "That password isn't right.", "password"))).toEqual({ locked: false, message: "That password isn't right." });
    expect(signInProblem(new ApiError(429, "login_locked", "Too many tries. Wait 15 minutes."))).toEqual({ locked: true, message: "Too many tries. Wait 15 minutes." });
  });
  it("says something plain when the server can't be reached", () => {
    expect(signInProblem(new NetworkError()).message).toMatch(/Can't reach Rectifi/);
    expect(signInProblem(new Error("boom")).locked).toBe(false);
  });
});
