import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, get } from "./client";

afterEach(() => vi.unstubAllGlobals());

function stubFetch(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
}

describe("the 401 hook", () => {
  it("tells the app to go to the login page when the server says auth_required", async () => {
    const heard = vi.fn();
    vi.stubGlobal("window", { dispatchEvent: heard });
    stubFetch(401, { error: { code: "auth_required", message: "Sign in first." } });
    await expect(get("/overview")).rejects.toBeInstanceOf(ApiError);
    expect(heard).toHaveBeenCalledTimes(1);
    expect((heard.mock.calls[0]?.[0] as Event).type).toBe("rectifi:auth-required");
  });

  it("stays quiet for other errors", async () => {
    const heard = vi.fn();
    vi.stubGlobal("window", { dispatchEvent: heard });
    stubFetch(422, { error: { code: "invalid", message: "No." } });
    await expect(get("/overview")).rejects.toBeInstanceOf(ApiError);
    expect(heard).not.toHaveBeenCalled();
  });
});
