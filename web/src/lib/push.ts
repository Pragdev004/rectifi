// Phone alerts: Web Push through the browser's own push service. Rectifi's laptop sends the message, the phone's Chrome shows it.
// Nothing here touches money. The permission question is only ever asked from a click (a button's handler calls enablePush).

export type PushState = "unsupported" | "needs-https" | "ios-needs-install" | "denied" | "off" | "on";

/** What the browser can do right now. Read once, then turned into a PushState by detectPushState (a pure function, easy to test). */
export interface PushEnv {
  secure: boolean;
  serviceWorker: boolean;
  pushManager: boolean;
  notification: boolean;
  permission: "default" | "granted" | "denied";
  ios: boolean;
  standalone: boolean;
  subscribed: boolean;
}

export function detectPushState(env: PushEnv): PushState {
  if (env.ios && !env.standalone) return "ios-needs-install";
  if (!env.secure) return "needs-https";
  if (!env.serviceWorker || !env.pushManager || !env.notification) return "unsupported";
  if (env.permission === "denied") return "denied";
  return env.subscribed ? "on" : "off";
}

export const STATE_TEXT: Record<PushState, { title: string; detail: string }> = {
  unsupported: { title: "This browser can't show phone alerts.", detail: "Open Rectifi in Chrome on your Android phone." },
  "needs-https": {
    title: "Phone alerts need the secure address.",
    detail: "Open the https:// address that Tailscale gave you (run dev.py tailscale on the laptop), not a plain http:// or IP address.",
  },
  "ios-needs-install": {
    title: "On an iPhone, add Rectifi to your Home Screen first.",
    detail: "Share, then Add to Home Screen. Open Rectifi from that icon and come back here. iPhone alerts have not been tested.",
  },
  denied: {
    title: "Notifications are blocked for Rectifi.",
    detail: "Allow notifications in the browser's site settings for this address, then come back and turn alerts on.",
  },
  off: { title: "Phone alerts are off on this phone.", detail: "Turn them on to get a message the moment a payment crosses one of your caps." },
  on: { title: "Phone alerts are on for this phone.", detail: "Press Send test to see one now." },
};

export function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

export interface Navigatorish {
  serviceWorker?: { getRegistration(scope?: string): Promise<{ pushManager: { getSubscription(): Promise<unknown | null> } } | undefined> };
  userAgent?: string;
  maxTouchPoints?: number;
  standalone?: boolean;
}
export interface Windowish {
  isSecureContext: boolean;
  Notification?: { permission: "default" | "granted" | "denied" };
  PushManager?: unknown;
  matchMedia?: (q: string) => { matches: boolean };
}

export async function readEnv(nav: Navigatorish, win: Windowish): Promise<PushEnv> {
  const serviceWorker = Boolean(nav.serviceWorker);
  let subscribed = false;
  if (serviceWorker && win.Notification?.permission === "granted") {
    try {
      const reg = await nav.serviceWorker!.getRegistration("/");
      subscribed = Boolean(reg && (await reg.pushManager.getSubscription()));
    } catch {
      subscribed = false;
    }
  }
  const ua = nav.userAgent ?? "";
  return {
    secure: win.isSecureContext,
    serviceWorker,
    pushManager: Boolean(win.PushManager),
    notification: Boolean(win.Notification),
    permission: win.Notification?.permission ?? "default",
    ios: /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (nav.maxTouchPoints ?? 0) > 1),
    standalone: nav.standalone === true || Boolean(win.matchMedia?.("(display-mode: standalone)").matches),
    subscribed,
  };
}

// ── turning alerts on ──

export interface PushDeps {
  requestPermission(): Promise<"default" | "granted" | "denied">;
  register(): Promise<{ pushManager: { subscribe(o: { userVisibleOnly: boolean; applicationServerKey: Uint8Array<ArrayBuffer> }): Promise<{ toJSON(): unknown }> } }>;
  getKey(): Promise<{ configured: boolean; public_key: string | null }>;
  saveSubscription(subscription: unknown): Promise<{ id: number }>;
}

export type EnableResult = { ok: true; id: number } | { ok: false; reason: "denied" | "no-key" };

/** Call this ONLY from a click handler. It asks permission, registers the push-only worker, subscribes and tells the laptop. */
export async function enablePush(deps: PushDeps): Promise<EnableResult> {
  const permission = await deps.requestPermission();
  if (permission !== "granted") return { ok: false, reason: "denied" };
  const key = await deps.getKey();
  if (!key.configured || !key.public_key) return { ok: false, reason: "no-key" };
  const registration = await deps.register();
  const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key.public_key) });
  const saved = await deps.saveSubscription(subscription.toJSON());
  return { ok: true, id: saved.id };
}

const STORED = "rectifi.push.subscription";

export function rememberSubscription(id: number | null): void {
  try {
    if (id === null) localStorage.removeItem(STORED);
    else localStorage.setItem(STORED, String(id));
  } catch {
    /* private window: the device list still lets you remove it */
  }
}

export function rememberedSubscription(): number | null {
  try {
    const raw = localStorage.getItem(STORED);
    return raw && /^\d+$/.test(raw) ? Number(raw) : null;
  } catch {
    return null;
  }
}

/** The real browser, for the page. Tests pass their own PushDeps instead. */
export function browserDeps(post: (path: string, body: unknown) => Promise<{ id: number }>, get: (path: string) => Promise<{ configured: boolean; public_key: string | null }>): PushDeps {
  return {
    requestPermission: () => Notification.requestPermission(),
    register: async () => {
      await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      return navigator.serviceWorker.ready;
    },
    getKey: () => get("/push/key"),
    saveSubscription: (subscription) => post("/push/subscriptions", { ...(subscription as object), label: deviceLabel() }),
  };
}

export function deviceLabel(): string {
  const ua = navigator.userAgent;
  const android = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iPhone" : /Windows/.test(ua) ? "Windows" : /Mac/.test(ua) ? "Mac" : "This device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : "browser";
  return `${android} ${browser}`;
}

export async function disablePush(): Promise<void> {
  const reg = await navigator.serviceWorker.getRegistration("/");
  const subscription = await reg?.pushManager.getSubscription();
  await subscription?.unsubscribe();
}
