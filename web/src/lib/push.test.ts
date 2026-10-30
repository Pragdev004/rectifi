import { describe, expect, it, vi } from "vitest";
import { detectPushState, enablePush, readEnv, STATE_TEXT, urlBase64ToUint8Array, type PushDeps, type PushEnv } from "./push";

const base: PushEnv = {
  secure: true, serviceWorker: true, pushManager: true, notification: true, permission: "default", ios: false, standalone: false, subscribed: false,
};

describe("urlBase64ToUint8Array", () => {
  it("decodes the url-safe alphabet and missing padding", () => {
    // 0xfb 0xff 0xfe is "-__-" in url-safe base64 ("+//+" in the normal one)
    expect(Array.from(urlBase64ToUint8Array("-__-"))).toEqual([0xfb, 0xff, 0xfe]);
    expect(Array.from(urlBase64ToUint8Array("AQID"))).toEqual([1, 2, 3]);
    expect(Array.from(urlBase64ToUint8Array("AQI"))).toEqual([1, 2]);
    expect(urlBase64ToUint8Array("").length).toBe(0);
  });
  it("turns a 65-byte public key into 65 bytes", () => {
    const key = "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8";
    const bytes = urlBase64ToUint8Array(key);
    expect(bytes.length).toBe(65);
    expect(bytes[0]).toBe(4); // an uncompressed P-256 point starts with 0x04
  });
});

describe("detectPushState", () => {
  const cases: [string, Partial<PushEnv>, string][] = [
    ["a normal Android Chrome that has not subscribed", {}, "off"],
    ["one that has", { permission: "granted", subscribed: true }, "on"],
    ["permission blocked", { permission: "denied" }, "denied"],
    ["plain http", { secure: false, serviceWorker: false, pushManager: false }, "needs-https"],
    ["no push support", { pushManager: false }, "unsupported"],
    ["no service worker", { serviceWorker: false }, "unsupported"],
    ["no Notification", { notification: false }, "unsupported"],
    ["iPhone in the browser", { ios: true }, "ios-needs-install"],
    ["iPhone installed", { ios: true, standalone: true }, "off"],
    ["iPhone in the browser, even over http", { ios: true, secure: false }, "ios-needs-install"],
  ];
  for (const [name, change, want] of cases) {
    it(`${name} is ${want}`, () => {
      expect(detectPushState({ ...base, ...change })).toBe(want);
    });
  }
  it("has plain words for every state", () => {
    for (const state of ["unsupported", "needs-https", "ios-needs-install", "denied", "off", "on"] as const) {
      expect(STATE_TEXT[state].title.length).toBeGreaterThan(10);
      expect(STATE_TEXT[state].detail.length).toBeGreaterThan(10);
    }
  });
});

describe("readEnv with a mocked navigator", () => {
  it("is subscribed only when permission is granted and the browser holds a subscription", async () => {
    const withSub = { serviceWorker: { getRegistration: async () => ({ pushManager: { getSubscription: async () => ({}) } }) }, userAgent: "Mozilla/5.0 (Linux; Android 14) Chrome/130" };
    const win = { isSecureContext: true, Notification: { permission: "granted" as const }, PushManager: function PushManager() {} };
    const env = await readEnv(withSub, win);
    expect(env).toMatchObject({ secure: true, serviceWorker: true, pushManager: true, notification: true, permission: "granted", ios: false, subscribed: true });
    expect(detectPushState(env)).toBe("on");
    const none = await readEnv({ ...withSub, serviceWorker: { getRegistration: async () => undefined } }, win);
    expect(detectPushState(none)).toBe("off");
  });
  it("sees an iPhone and the installed app", async () => {
    const ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)";
    const win = { isSecureContext: true, Notification: { permission: "default" as const }, PushManager: {}, matchMedia: () => ({ matches: true }) };
    expect(await readEnv({ userAgent: ua, serviceWorker: {} as never }, win)).toMatchObject({ ios: true, standalone: true });
    expect(await readEnv({ userAgent: ua }, { ...win, matchMedia: () => ({ matches: false }) })).toMatchObject({ ios: true, standalone: false });
  });
  it("on plain http there is no service worker object at all", async () => {
    const env = await readEnv({ userAgent: "Chrome" }, { isSecureContext: false });
    expect(detectPushState(env)).toBe("needs-https");
  });
});

describe("enablePush", () => {
  function deps(permission: "default" | "granted" | "denied", key = { configured: true, public_key: "AQID" }) {
    const subscribe = vi.fn(async () => ({ toJSON: () => ({ endpoint: "https://fcm.googleapis.com/x", keys: { p256dh: "a", auth: "b" } }) }));
    const d: PushDeps & { calls: string[] } = {
      calls: [],
      requestPermission: vi.fn(async () => {
        d.calls.push("permission");
        return permission;
      }),
      register: vi.fn(async () => {
        d.calls.push("register");
        return { pushManager: { subscribe } };
      }),
      getKey: vi.fn(async () => key),
      saveSubscription: vi.fn(async () => ({ id: 7 })),
    };
    return { d, subscribe };
  }

  it("asks permission once, registers, subscribes with the server key and saves the result", async () => {
    const { d, subscribe } = deps("granted");
    expect(await enablePush(d)).toEqual({ ok: true, id: 7 });
    expect(d.requestPermission).toHaveBeenCalledTimes(1);
    expect(d.calls).toEqual(["permission", "register"]);
    const options = (subscribe.mock.calls[0] as unknown as [{ userVisibleOnly: boolean; applicationServerKey: Uint8Array }])[0];
    expect(options.userVisibleOnly).toBe(true);
    expect(Array.from(options.applicationServerKey)).toEqual([1, 2, 3]);
    expect(d.saveSubscription).toHaveBeenCalledWith({ endpoint: "https://fcm.googleapis.com/x", keys: { p256dh: "a", auth: "b" } });
  });
  it("stops at a refused permission without registering anything", async () => {
    const { d } = deps("denied");
    expect(await enablePush(d)).toEqual({ ok: false, reason: "denied" });
    expect(d.register).not.toHaveBeenCalled();
    expect(d.saveSubscription).not.toHaveBeenCalled();
    const dismissed = deps("default");
    expect(await enablePush(dismissed.d)).toEqual({ ok: false, reason: "denied" });
  });
  it("does not subscribe when the laptop has no push key", async () => {
    const { d } = deps("granted", { configured: false, public_key: null });
    expect(await enablePush(d)).toEqual({ ok: false, reason: "no-key" });
    expect(d.register).not.toHaveBeenCalled();
  });
});
