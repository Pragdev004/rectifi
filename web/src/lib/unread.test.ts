import { describe, expect, it } from "vitest";
import { unreadSpoken, unreadWords } from "./unread";

describe("unread chip words", () => {
  it("says the number with the word new", () => {
    expect(unreadWords(3)).toBe("3 new");
    expect(unreadWords(1)).toBe("1 new");
  });
  it("shows nothing when nothing is unread or the count is not known yet", () => {
    expect(unreadWords(0)).toBeNull();
    expect(unreadWords(undefined)).toBeNull();
    expect(unreadWords(null)).toBeNull();
  });
  it("does not let a huge count break the menu", () => {
    expect(unreadWords(250)).toBe("99+ new");
    expect(unreadWords(99)).toBe("99 new");
  });
  it("has a full sentence for screen readers", () => {
    expect(unreadSpoken(1)).toBe("1 new alert");
    expect(unreadSpoken(3)).toBe("3 new alerts");
    expect(unreadSpoken(0)).toBeNull();
  });
});
