import { describe, expect, it } from "vitest";
import type { AuthStatus } from "../../api/auth";
import { MORE_ITEMS, visibleGroups, visibleItems } from "./nav";

const status = (over: Partial<AuthStatus>): AuthStatus => ({ required: true, signed_in: true, multiuser: true, username: "x", is_owner: false, ...over });
const labels = (s: AuthStatus | undefined) => visibleGroups(s).flatMap((g) => g.items.map((i) => i.label));
const OWNER_ONLY = ["Ask", "Charts", "Library", "Memory", "Traces"];

describe("the menu for each kind of person", () => {
  it("hides Ask, Charts, Library, Memory and Traces from a friend and keeps the rest", () => {
    const friend = labels(status({ is_owner: false }));
    for (const name of OWNER_ONLY) expect(friend).not.toContain(name);
    for (const name of ["Overview", "Review", "Insights", "Alerts", "Ledger", "Leaks", "Budgets", "Accounts", "Phone", "Settings"]) expect(friend).toContain(name);
  });
  it("shows the owner everything, with Settings", () => {
    const owner = labels(status({ is_owner: true }));
    for (const name of [...OWNER_ONLY, "Settings", "Overview"]) expect(owner).toContain(name);
  });
  it("shows a laptop everything except Settings (no accounts there)", () => {
    const laptop = labels(status({ multiuser: false, is_owner: true, required: false }));
    for (const name of OWNER_ONLY) expect(laptop).toContain(name);
    expect(laptop).not.toContain("Settings");
  });
  it("shows no Settings before you are signed in, and keeps the owner pages in view until the server has answered", () => {
    expect(labels(status({ signed_in: false, username: null }))).not.toContain("Settings");
    expect(labels(undefined)).toContain("Ask");
  });
  it("filters the More sheet the same way", () => {
    const friend = visibleItems(MORE_ITEMS, status({ is_owner: false })).map((i) => i.label);
    for (const name of OWNER_ONLY) expect(friend).not.toContain(name);
    expect(friend).toContain("Settings");
  });
});
