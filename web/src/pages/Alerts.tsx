import { useMutation, useQueryClient } from "@tanstack/react-query";
import { BellRing, Lightbulb, Send, Trash2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "wouter";
import {
  alertKeys, useAlerts, useDevices, usePatterns, usePrefs, usePresets, usePushKey, useRules,
  type AlertItem, type AlertPrefs, type AlertRule, type Preset, type RulePeriod, type RuleScope,
} from "../api/alerts";
import { useChatStatus } from "../api/chat";
import { ApiError, del, patch, post, put } from "../api/client";
import type { ExplainResult } from "../api/types";
import { Button, Checkbox, Field, panelId, SegmentedControl, Select, Tabs, tabId, TextInput } from "../components/forms";
import { ExplainNote } from "../components/InsightRow";
import { Loaded } from "../components/Loaded";
import { ModelCheck } from "../components/ModelCheck";
import { Money } from "../components/Money";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, EmptyState, InlineStatus, Section, Skeleton, Stamp, type Tone } from "../components/ui";
import { useMeta } from "../api/queries";
import { browserDeps, detectPushState, disablePush, enablePush, readEnv, rememberedSubscription, rememberSubscription, STATE_TEXT, type PushState } from "../lib/push";
import { formatInr } from "../lib/money";
import { useUrlState } from "../lib/urlState";
import styles from "./alerts.module.css";
import pages from "./pages.module.css";

type Tab = "inbox" | "caps" | "delivery";
const TABS: { id: Tab; label: string }[] = [
  { id: "inbox", label: "Inbox" },
  { id: "caps", label: "Caps" },
  { id: "delivery", label: "Delivery" },
];

const INTRO =
  "Rectifi watches every payment as it arrives and tells you when a cap is nearly used up. Fixed rules work out every figure on your laptop: " +
  "no AI decides a number, and nothing leaves your laptop except a short notification to your own phone.";

function message(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

/** After a change to caps, alerts or settings, everything that shows them is out of date. */
function useRefresh() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ["alerts"] });
    void client.invalidateQueries({ queryKey: ["leaks"] });
  };
}

// ───────────────────────── inbox ─────────────────────────

const SEVERITY: Record<AlertItem["severity"], { tone: Tone; word: string }> = {
  act: { tone: "over", word: "Act" },
  watch: { tone: "near", word: "Watch" },
  info: { tone: "accent", word: "Note" },
};

const stamp = (iso: string) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

function AlertRow({ item, explainOk }: { item: AlertItem; explainOk: boolean }) {
  const refresh = useRefresh();
  const toast = useToast();
  const [explained, setExplained] = useState<ExplainResult | null>(null);
  const sev = SEVERITY[item.severity];
  const failed = (e: unknown) => toast(message(e, "That did not work."), "over");

  const read = useMutation({ mutationFn: () => post(`/alerts/${item.id}/read`), onSuccess: refresh, onError: failed });
  const mute = useMutation({
    mutationFn: () => post(`/alerts/${item.id}/mute`),
    onSuccess: () => { toast("Muted. You can turn it back on under Caps."); refresh(); },
    onError: failed,
  });
  const snooze = useMutation({
    mutationFn: (hours: number) => post(`/alerts/${item.id}/snooze`, { hours }),
    onSuccess: () => { toast("Snoozed."); refresh(); },
    onError: failed,
  });
  const explain = useMutation({
    mutationFn: () => post<ExplainResult>(`/alerts/${item.id}/explain`),
    onSuccess: setExplained,
    onError: (e) => setExplained({ text: null, dropped: [], error: message(e, "The assistant could not answer.") }),
  });

  return (
    <li id={`alert-${item.id}`} className={styles.alert} data-testid="alert-row" data-read={item.is_read} data-kind={item.kind}>
      <div className={styles.stampCol}><Stamp tone={sev.tone}>{sev.word}</Stamp>{!item.is_read && <span className={styles.newWord}>New</span>}</div>
      <div>
        <p className={styles.alertTitle}>{item.title}</p>
        <p className={styles.alertDetail}>{item.detail}</p>
        <p className={styles.when}>{stamp(item.created_at)}</p>
        <div className={styles.actions}>
          {!item.is_read && <Button variant="secondary" loading={read.isPending} loadingLabel="Saving…" onClick={() => read.mutate()}>Read</Button>}
          {item.can_snooze && (
            <Select aria-label={`Snooze alert ${item.title}`} className={styles.snooze} value="" onChange={(e) => e.target.value && snooze.mutate(Number(e.target.value))}>
              <option value="">Snooze…</option>
              <option value="1">For 1 hour</option>
              <option value="24">For 1 day</option>
              <option value="168">For 1 week</option>
            </Select>
          )}
          <Button variant="tertiary" loading={mute.isPending} loadingLabel="Muting…" onClick={() => mute.mutate()}>Mute</Button>
          {explainOk && item.kind !== "bulk" && (
            <Button variant="tertiary" loading={explain.isPending} loadingLabel="Thinking…" icon={<Lightbulb size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => explain.mutate()}>
              Explain this
            </Button>
          )}
        </div>
        {explained && <ExplainNote result={explained} />}
      </div>
    </li>
  );
}

function Inbox() {
  const query = useAlerts("all");
  const chat = useChatStatus();
  const refresh = useRefresh();
  const toast = useToast();
  const readAll = useMutation({ mutationFn: () => post<{ marked: number }>("/alerts/read-all"), onSuccess: refresh, onError: (e) => toast(message(e, "That did not work."), "over") });
  const explainOk = chat.data?.ok === true;

  useEffect(() => {
    const id = window.location.hash.replace("#", "");
    if (id && query.data) document.getElementById(`alert-${id.replace(/\D/g, "")}`)?.scrollIntoView({ block: "center" });
  }, [query.data]);

  return (
    <Loaded query={query} skeleton={<div className={pages.skelStack} aria-hidden="true"><Skeleton height={90} /><Skeleton height={90} /></div>}>
      {(data) => {
        if (data.items.length === 0) {
          return (
            <EmptyState title="No alerts yet." actions={<Button variant="primary" onClick={() => undefined} className={styles.hidden} aria-hidden="true" tabIndex={-1}>{""}</Button>}>
              Set a cap under Caps (try Swiggy for a week) and Rectifi will tell you here, and on your phone, when a payment brings you close to it.
            </EmptyState>
          );
        }
        return (
          <>
            <div className={styles.inboxHead}>
              <p className={pages.muted}>{data.unread > 0 ? `${data.unread} ${data.unread === 1 ? "alert is" : "alerts are"} new.` : "Everything is read."}</p>
              {data.unread > 0 && <Button variant="secondary" loading={readAll.isPending} loadingLabel="Saving…" onClick={() => readAll.mutate()}>Mark all read</Button>}
            </div>
            <ul className={styles.alerts}>{data.items.map((i) => <AlertRow key={i.id} item={i} explainOk={explainOk} />)}</ul>
            {!explainOk && chat.data && <ModelCheck />}
          </>
        );
      }}
    </Loaded>
  );
}

// ───────────────────────── caps ─────────────────────────

const PERIOD_WORD: Record<RulePeriod, string> = { day: "a day", week: "a week", month: "a month" };
const PERIOD_OPTIONS: { value: RulePeriod; label: string }[] = [
  { value: "day", label: "Day" },
  { value: "week", label: "Week" },
  { value: "month", label: "Month" },
];
const SCOPE_WORD: Record<RuleScope, string> = { merchant: "Merchant", group: "Group", category: "Category", total: "All spending" };

function ruleName(r: AlertRule) {
  return r.scope === "total" ? "All spending" : r.target;
}

function RuleRow({ rule }: { rule: AlertRule }) {
  const refresh = useRefresh();
  const toast = useToast();
  const [cap, setCap] = useState(rule.cap.replace(/\.00$/, ""));
  const failed = (e: unknown) => toast(message(e, "That did not work."), "over");
  const save = useMutation({ mutationFn: () => patch(`/alerts/rules/${rule.id}`, { cap }), onSuccess: () => { toast("Cap saved."); refresh(); }, onError: failed });
  const toggle = useMutation({ mutationFn: (muted: boolean) => patch(`/alerts/rules/${rule.id}`, { muted }), onSuccess: refresh, onError: failed });
  const remove = useMutation({ mutationFn: () => del(`/alerts/rules/${rule.id}`), onSuccess: () => { toast("Cap removed."); refresh(); }, onError: failed });
  return (
    <li className={styles.rule} data-testid="rule-row" data-target={rule.target} data-period={rule.period}>
      <div className={styles.ruleMain}>
        <p className={styles.ruleName}>{ruleName(rule)} {rule.muted && <Stamp tone="neutral">Muted</Stamp>}{rule.snoozed_until && !rule.muted && <Stamp tone="near">Snoozed</Stamp>}</p>
        <p className={pages.muted}>{SCOPE_WORD[rule.scope]} {"·"} at most <Money value={rule.cap} paise={false} /> {PERIOD_WORD[rule.period]}</p>
      </div>
      <form className={styles.ruleEdit} onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <Field label="Cap in rupees" hideLabel>{(p) => <TextInput {...p} value={cap} inputMode="decimal" onChange={(e) => setCap(e.target.value)} className={styles.capInput} />}</Field>
        <Button type="submit" variant="secondary" loading={save.isPending} loadingLabel="Saving…">Save</Button>
      </form>
      <div className={styles.ruleActions}>
        <Checkbox label="Alerts on" checked={!rule.muted} onChange={(on) => toggle.mutate(!on)} />
        <Button variant="dangerQuiet" aria-label={`Remove the cap on ${ruleName(rule)}`} icon={<Trash2 size={16} strokeWidth={1.5} aria-hidden="true" />} loading={remove.isPending} loadingLabel="Removing…" onClick={() => remove.mutate()}>Remove</Button>
      </div>
    </li>
  );
}

function PresetRow({ preset, period, taken }: { preset: Preset; period: RulePeriod; taken: boolean }) {
  const refresh = useRefresh();
  const toast = useToast();
  const make = useMutation({
    mutationFn: (cap: string) => post("/alerts/rules", { scope: preset.scope, target: preset.target, period, cap }),
    onSuccess: () => { toast(`Cap set for ${preset.target}.`); refresh(); },
    onError: (e) => toast(message(e, "Could not set that cap."), "over"),
  });
  return (
    <li className={styles.preset} data-testid="preset-row" data-target={preset.target} data-scope={preset.scope}>
      <div>
        <p className={styles.ruleName}>{preset.target}</p>
        <p className={pages.muted}>You spent about <Money value={preset.average[period]} paise={false} /> {PERIOD_WORD[period]} over the last {preset.months} {preset.months === 1 ? "month" : "months"}.</p>
      </div>
      <div className={styles.presetButtons}>
        <Button variant="secondary" disabled={make.isPending} onClick={() => make.mutate(preset.keep[period])} aria-label={`Keep ${formatInr(preset.keep[period], { paise: false })} ${PERIOD_WORD[period]} for ${preset.target}`}>
          Keep {formatInr(preset.keep[period], { paise: false })}
        </Button>
        <Button variant="secondary" disabled={make.isPending} onClick={() => make.mutate(preset.trim[period])} aria-label={`Trim 20% to ${formatInr(preset.trim[period], { paise: false })} ${PERIOD_WORD[period]} for ${preset.target}`}>
          Trim 20%: {formatInr(preset.trim[period], { paise: false })}
        </Button>
        {taken && <span className={styles.taken}>A cap exists. Pressing replaces it.</span>}
      </div>
    </li>
  );
}

function CustomCap({ suggestions }: { suggestions: string[] }) {
  const refresh = useRefresh();
  const toast = useToast();
  const [scope, setScope] = useState<RuleScope>("merchant");
  const [target, setTarget] = useState("");
  const [cap, setCap] = useState("");
  const [period, setPeriod] = useState<RulePeriod>("week");
  const [error, setError] = useState<string | null>(null);
  const make = useMutation({
    mutationFn: () => post("/alerts/rules", { scope, target: scope === "total" ? "all" : target, period, cap }),
    onSuccess: () => { toast("Cap set."); setTarget(""); setCap(""); setError(null); refresh(); },
    onError: (e) => setError(message(e, "Could not set that cap.")),
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    make.mutate();
  }
  return (
    <form className={styles.custom} onSubmit={submit} aria-label="Set your own cap">
      <Field label="What to watch">
        {(p) => (
          <Select {...p} value={scope} onChange={(e) => setScope(e.target.value as RuleScope)}>
            {(Object.keys(SCOPE_WORD) as RuleScope[]).map((s) => <option key={s} value={s}>{SCOPE_WORD[s]}</option>)}
          </Select>
        )}
      </Field>
      {scope !== "total" && (
        <Field label={scope === "merchant" ? "Merchant name" : scope === "group" ? "Group name" : "Category name"} hint="For example Swiggy, Food delivery or Groceries.">
          {(p) => <TextInput {...p} list="cap-suggestions" value={target} onChange={(e) => setTarget(e.target.value)} autoComplete="off" />}
        </Field>
      )}
      <datalist id="cap-suggestions">{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
      <Field label="At most, in rupees">{(p) => <TextInput {...p} value={cap} inputMode="decimal" onChange={(e) => setCap(e.target.value)} placeholder="1500" />}</Field>
      <SegmentedControl legend="How often" name="custom-period" options={PERIOD_OPTIONS} value={period} onChange={setPeriod} />
      {error && <p className={styles.error} role="alert">{error}</p>}
      <div><Button type="submit" variant="primary" loading={make.isPending} loadingLabel="Setting…">Set cap</Button></div>
    </form>
  );
}

function MerchantNames() {
  const query = usePatterns();
  const refresh = useRefresh();
  const toast = useToast();
  const [pattern, setPattern] = useState("");
  const [merchant, setMerchant] = useState("");
  const [group, setGroup] = useState("Other");
  const [error, setError] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: () => post("/merchants/patterns", { pattern, merchant, group }),
    onSuccess: () => { toast("Merchant name saved."); setPattern(""); setMerchant(""); setError(null); refresh(); },
    onError: (e) => setError(message(e, "Could not save that name.")),
  });
  const remove = useMutation({ mutationFn: (id: number) => del(`/merchants/patterns/${id}`), onSuccess: refresh, onError: (e) => toast(message(e, "That did not work."), "over") });
  return (
    <Section eyebrow="Add a merchant name">
      <p className={pages.muted}>Rectifi already knows Swiggy, Zomato, Blinkit, Zepto, Amazon and a few more. If a shop shows up under another name in your bank SMS, teach it here.</p>
      <Loaded query={query} skeleton={<Skeleton height={80} />}>
        {(data) => (
          <>
            <form className={styles.custom} onSubmit={(e) => { e.preventDefault(); setError(null); add.mutate(); }} aria-label="Add a merchant name">
              <Field label="Text that appears in the payment" hint="At least 3 letters, like kiranakart.">{(p) => <TextInput {...p} value={pattern} onChange={(e) => setPattern(e.target.value)} autoComplete="off" />}</Field>
              <Field label="Show it as">{(p) => <TextInput {...p} value={merchant} onChange={(e) => setMerchant(e.target.value)} autoComplete="off" />}</Field>
              <Field label="Group">
                {(p) => <Select {...p} value={group} onChange={(e) => setGroup(e.target.value)}>{data.groups.map((g) => <option key={g} value={g}>{g}</option>)}</Select>}
              </Field>
              {error && <p className={styles.error} role="alert">{error}</p>}
              <div><Button type="submit" variant="secondary" loading={add.isPending} loadingLabel="Saving…">Save name</Button></div>
            </form>
            {data.items.length > 0 && (
              <ul className={styles.names}>
                {data.items.map((p) => (
                  <li key={p.id}>
                    <span><strong>{p.merchant}</strong> {"·"} {p.group} {"·"} matches {"“"}{p.pattern}{"”"}</span>
                    <Button variant="dangerQuiet" aria-label={`Remove the name ${p.merchant}`} onClick={() => remove.mutate(p.id)}>Remove</Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Loaded>
    </Section>
  );
}

function Caps() {
  const rules = useRules();
  const presets = usePresets();
  const patterns = usePatterns();
  const meta = useMeta();
  const [period, setPeriod] = useState<RulePeriod>("week");
  const taken = new Set((rules.data?.items ?? []).filter((r) => r.period === period).map((r) => `${r.scope}:${r.target}`));
  const suggestions = [
    ...new Set([
      ...(presets.data?.items ?? []).filter((p) => p.scope === "merchant").map((p) => p.target),
      ...(patterns.data?.items ?? []).map((p) => p.merchant),
      "Swiggy", "Zomato", "Blinkit", "Zepto", "Amazon", "Quick commerce", "Food delivery", "Shopping",
      ...(meta.data?.categories ?? []).filter((c) => c.kind === "expense").map((c) => c.name),
    ]),
  ];
  const groups: { scope: RuleScope; heading: string }[] = [
    { scope: "merchant", heading: "Merchants" },
    { scope: "group", heading: "Groups" },
    { scope: "category", heading: "Categories" },
  ];
  return (
    <>
      <Section eyebrow="Your caps">
        <Loaded query={rules} skeleton={<Skeleton height={90} />}>
          {(data) =>
            data.items.length === 0 ? (
              <p className={pages.muted}>No caps yet. Pick one from your own spending below, or set your own. Your monthly budgets also raise alerts without any cap here.</p>
            ) : (
              <ul className={styles.rules}>{data.items.map((r) => <RuleRow key={r.id} rule={r} />)}</ul>
            )
          }
        </Loaded>
      </Section>
      <Section eyebrow="Set a cap from your average">
        <SegmentedControl legend="Cap for each" name="preset-period" options={PERIOD_OPTIONS} value={period} onChange={setPeriod} />
        <Loaded query={presets} skeleton={<Skeleton height={120} />}>
          {(data) =>
            data.items.length === 0 ? (
              <p className={pages.muted}>Presets need at least one complete month of spending. Until then, set your own cap below.</p>
            ) : (
              <>
                {groups.map((g) => {
                  const items = data.items.filter((p) => p.scope === g.scope).slice(0, 8);
                  return items.length === 0 ? null : (
                    <div key={g.scope}>
                      <p className={styles.groupHead}>{g.heading}</p>
                      <ul className={styles.presets}>{items.map((p) => <PresetRow key={`${p.scope}:${p.target}`} preset={p} period={period} taken={taken.has(`${p.scope}:${p.target}`)} />)}</ul>
                    </div>
                  );
                })}
              </>
            )
          }
        </Loaded>
      </Section>
      <Section eyebrow="Set your own cap"><CustomCap suggestions={suggestions} /></Section>
      <MerchantNames />
    </>
  );
}

// ───────────────────────── delivery ─────────────────────────

function PhoneAlerts() {
  const key = usePushKey();
  const devices = useDevices();
  const refresh = useRefresh();
  const toast = useToast();
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  async function look() {
    setState(detectPushState(await readEnv(navigator, window as unknown as Parameters<typeof readEnv>[1])));
  }
  useEffect(() => { void look(); }, []);

  async function turnOn() {
    setBusy(true);
    try {
      const result = await enablePush(browserDeps((path, body) => post<{ id: number }>(path, body), (path) => import("../api/client").then((c) => c.get(path))));
      if (result.ok) {
        rememberSubscription(result.id);
        toast("Phone alerts are on.");
      } else if (result.reason === "no-key") {
        toast("Rectifi could not make its push key. See the message under the buttons.", "over");
      }
    } catch (e) {
      toast(message(e, "Could not turn alerts on."), "over");
    } finally {
      setBusy(false);
      refresh();
      await look();
    }
  }
  async function turnOff() {
    setBusy(true);
    try {
      await disablePush();
      const id = rememberedSubscription();
      if (id !== null) await del(`/push/subscriptions/${id}`).catch(() => undefined);
      rememberSubscription(null);
      toast("Phone alerts are off on this phone.", "note");
    } catch (e) {
      toast(message(e, "Could not turn alerts off."), "over");
    } finally {
      setBusy(false);
      refresh();
      await look();
    }
  }
  const test = useMutation({
    mutationFn: () => post<{ devices: number; ok: number; gone: number; retry: number }>("/push/test"),
    onSuccess: (r) => { toast(r.ok > 0 ? "Sent. Look at your phone." : "The push service did not accept it. Try again in a minute.", r.ok > 0 ? "ok" : "over"); refresh(); },
    onError: (e) => toast(message(e, "Could not send a test."), "over"),
  });
  const remove = useMutation({ mutationFn: (id: number) => del(`/push/subscriptions/${id}`), onSuccess: () => { if (rememberedSubscription() === null) return; refresh(); }, onSettled: refresh, onError: (e) => toast(message(e, "That did not work."), "over") });

  const words = state ? STATE_TEXT[state] : null;
  return (
    <Section eyebrow="Alerts on your phone">
      {words && (
        <Callout tone={state === "on" ? "ok" : state === "off" ? "note" : "near"} lead={words.title} icon={BellRing}>
          {words.detail}
        </Callout>
      )}
      <div className={styles.actions}>
        {state === "off" && <Button variant="primary" loading={busy} loadingLabel="Turning on…" onClick={() => void turnOn()} icon={<BellRing size={16} strokeWidth={1.5} aria-hidden="true" />}>Turn on phone alerts</Button>}
        {state === "on" && <Button variant="secondary" loading={busy} loadingLabel="Turning off…" onClick={() => void turnOff()}>Turn off on this phone</Button>}
        <Button variant="secondary" loading={test.isPending} loadingLabel="Sending…" icon={<Send size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => test.mutate()} disabled={(devices.data?.items.length ?? 0) === 0}>Send test</Button>
      </div>
      <Loaded query={devices} skeleton={<Skeleton height={60} />}>
        {(data) =>
          data.items.length === 0 ? (
            <p className={pages.muted}>No phone is set up yet.</p>
          ) : (
            <ul className={styles.names} aria-label="Phones with alerts on">
              {data.items.map((d) => (
                <li key={d.id} data-testid="device-row">
                  <span><strong>{d.label ?? "A phone"}</strong> {"·"} {d.service} {"·"} {d.last_ok_at ? `last worked ${stamp(d.last_ok_at)}` : "not used yet"}</span>
                  <Button variant="dangerQuiet" aria-label={`Remove ${d.label ?? "this phone"}`} onClick={() => remove.mutate(d.id)}>Remove</Button>
                </li>
              ))}
            </ul>
          )
        }
      </Loaded>
      {key.data && key.data.configured && (
        <p className={pages.muted}>Push key fingerprint: <code>{key.data.fingerprint}</code>. If this ever changes, turn alerts on again on each phone.</p>
      )}
      {key.data && !key.data.configured && <p className={styles.error} role="alert">Rectifi could not make or read its push key. Set RECTIFI_VAPID_PRIVATE_KEY, or check that the .rectifi folder is writable.</p>}
    </Section>
  );
}

const COUNTS = Array.from({ length: 20 }, (_, i) => String(i + 1));

function Settings({ prefs }: { prefs: AlertPrefs }) {
  const refresh = useRefresh();
  const [form, setForm] = useState({
    push_on: prefs.push_on, private_notifications: prefs.private_notifications, quiet_start: prefs.quiet_start, quiet_end: prefs.quiet_end,
    max_per_hour: String(prefs.max_per_hour), max_per_day: String(prefs.max_per_day), cooldown_hours: String(prefs.cooldown_hours),
    digest_on: prefs.digest_on, digest_time: prefs.digest_time,
    monthly_income: prefs.monthly_income?.replace(/\.00$/, "") ?? "", spend_plan: prefs.spend_plan?.replace(/\.00$/, "") ?? "",
  });
  const [status, setStatus] = useState<{ kind: "saving" | "saved" | "error"; text: string } | null>(null);
  const set = <K extends keyof typeof form>(k: K, v: (typeof form)[K]) => { setForm((f) => ({ ...f, [k]: v })); setStatus(null); };
  const save = useMutation({
    mutationFn: () => put<AlertPrefs>("/alerts/prefs", {
      ...form, max_per_hour: Number(form.max_per_hour), max_per_day: Number(form.max_per_day), cooldown_hours: Number(form.cooldown_hours),
    }),
    onMutate: () => setStatus({ kind: "saving", text: "Saving…" }),
    onSuccess: () => { setStatus({ kind: "saved", text: "Saved." }); refresh(); },
    onError: (e) => setStatus({ kind: "error", text: e instanceof ApiError ? e.message : "Could not save." }),
  });
  return (
    <Section eyebrow="Settings">
      <form className={styles.settings} onSubmit={(e) => { e.preventDefault(); save.mutate(); }} aria-label="Alert settings">
        <Checkbox label="Send alerts to my phone" checked={form.push_on} onChange={(v) => set("push_on", v)} />
        <Checkbox label="Private notifications (the phone only says {“}New spending alert{”}, no amounts or names)" checked={form.private_notifications} onChange={(v) => set("private_notifications", v)} />
        <fieldset className={styles.pair}>
          <legend>Quiet hours</legend>
          <Field label="From">{(p) => <TextInput {...p} type="time" value={form.quiet_start} onChange={(e) => set("quiet_start", e.target.value)} />}</Field>
          <Field label="Until">{(p) => <TextInput {...p} type="time" value={form.quiet_end} onChange={(e) => set("quiet_end", e.target.value)} />}</Field>
          <p className={pages.muted}>Alerts that arrive in this time wait, and reach you as one message when it ends.</p>
        </fieldset>
        <div className={styles.triple}>
          <Field label="At most per hour">{(p) => <Select {...p} value={form.max_per_hour} onChange={(e) => set("max_per_hour", e.target.value)}>{COUNTS.map((n) => <option key={n}>{n}</option>)}</Select>}</Field>
          <Field label="At most per day">{(p) => <Select {...p} value={form.max_per_day} onChange={(e) => set("max_per_day", e.target.value)}>{COUNTS.map((n) => <option key={n}>{n}</option>)}</Select>}</Field>
          <Field label="Wait between alerts on one cap (hours)" hint="A 100% alert ignores this.">{(p) => <TextInput {...p} inputMode="numeric" value={form.cooldown_hours} onChange={(e) => set("cooldown_hours", e.target.value)} />}</Field>
        </div>
        <fieldset className={styles.pair}>
          <legend>Evening summary</legend>
          <Checkbox label="Send one summary of the day" checked={form.digest_on} onChange={(v) => set("digest_on", v)} />
          <Field label="At">{(p) => <TextInput {...p} type="time" value={form.digest_time} onChange={(e) => set("digest_time", e.target.value)} />}</Field>
        </fieldset>
        <Field
          label="Monthly income, in rupees"
          hint={prefs.detected_income ? <>Detected from your salary: {formatInr(prefs.detected_income, { paise: false })}. {form.monthly_income === "" ? "It is used while this box is empty." : "What you type here wins."}</> : "Leave empty and Rectifi uses your salary credits when it can find them."}
        >
          {(p) => <TextInput {...p} inputMode="decimal" value={form.monthly_income} onChange={(e) => set("monthly_income", e.target.value)} placeholder="100000" />}
        </Field>
        {prefs.detected_income && form.monthly_income !== prefs.detected_income.replace(/\.00$/, "") && (
          <div><Button variant="tertiary" onClick={() => set("monthly_income", prefs.detected_income!.replace(/\.00$/, ""))}>Use the detected amount</Button></div>
        )}
        <Field label="Spending plan for the month, in rupees (optional)" hint="What you want to spend in a month. Empty means Rectifi adds up your budgets, then falls back to your income.">
          {(p) => <TextInput {...p} inputMode="decimal" value={form.spend_plan} onChange={(e) => set("spend_plan", e.target.value)} />}
        </Field>
        <div className={styles.saveRow}>
          <Button type="submit" variant="primary" loading={save.isPending} loadingLabel="Saving…">Save settings</Button>
          {status && <InlineStatus kind={status.kind}>{status.text}</InlineStatus>}
        </div>
      </form>
    </Section>
  );
}

function Delivery() {
  const prefs = usePrefs();
  return (
    <>
      <PhoneAlerts />
      <Loaded query={prefs} skeleton={<Skeleton height={240} />}>{(data) => <Settings prefs={data} />}</Loaded>
    </>
  );
}

// ───────────────────────── page ─────────────────────────

export default function AlertsPage() {
  const [url, setUrl] = useUrlState({ tab: "inbox" });
  const tab = (TABS.some((t) => t.id === url.tab) ? url.tab : "inbox") as Tab;
  return (
    <>
      <PageHeader name="Alerts" title="Leak alerts" sub={INTRO} />
      <Tabs group="alerts" label="Alerts sections" tabs={TABS} value={tab} onChange={(id) => setUrl({ tab: id }, { replace: true })} />
      <div role="tabpanel" id={panelId("alerts", tab)} aria-labelledby={tabId("alerts", tab)} className={styles.panel}>
        {tab === "inbox" && <Inbox />}
        {tab === "caps" && <Caps />}
        {tab === "delivery" && <Delivery />}
      </div>
      <p className={pages.muted}><Link href="/leaks">See where the month{"’"}s money went</Link></p>
    </>
  );
}
