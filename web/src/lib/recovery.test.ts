import { describe, expect, it } from "vitest";
import { canFinish, recoveryFileName, recoveryFileText } from "./recovery";

describe("the recovery code gate", () => {
  it("is shut until the code is downloaded or the box is ticked", () => {
    expect(canFinish({ downloaded: false, saved: false })).toBe(false);
    expect(canFinish({ downloaded: true, saved: false })).toBe(true);
    expect(canFinish({ downloaded: false, saved: true })).toBe(true);
    expect(canFinish({ downloaded: true, saved: true })).toBe(true);
  });
});

describe("the recovery file", () => {
  it("holds the code and says what it is for", () => {
    const text = recoveryFileText("asha", "ABCD-EFGH-JKLM");
    expect(text).toContain("ABCD-EFGH-JKLM");
    expect(text).toContain("asha");
    expect(text).toMatch(/only way back/i);
    expect(text).toMatch(/no email and no reset/i);
  });
  it("is named from a safe version of the username", () => {
    expect(recoveryFileName("asha")).toBe("rectifi-recovery-code-asha.txt");
    expect(recoveryFileName("../../evil name")).toBe("rectifi-recovery-code-.._.._evil_name.txt");
    expect(recoveryFileName("")).toBe("rectifi-recovery-code-account.txt");
    expect(recoveryFileName("x".repeat(80)).length).toBeLessThan(80);
  });
});
