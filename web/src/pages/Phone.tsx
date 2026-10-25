import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, Download, EyeOff, FlaskConical, KeyRound, Link2, NotebookPen, Send, X } from "lucide-react";
import { useState } from "react";
import { ApiError, post } from "../api/client";
import { keys, usePhoneDevices, usePhoneInbox, usePhoneStatus } from "../api/queries";
import type { Device, InboxItem, PairResult, PhoneStatus } from "../api/types";
import { Button, Field, LinkButton, TextInput } from "../components/forms";
import { ConfirmDialog } from "../components/Dialog";
import { Loaded } from "../components/Loaded";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { Callout, CodeValue, Disclosure, EmptyState, Section, Skeleton, Stamp } from "../components/ui";
import { fmtDateTime, timeAgo } from "../lib/dates";
import styles from "./pages.module.css";
import p from "./phone.module.css";

const NAME_MAX = 60;

// ── 1. open Rectifi on your phone ──

function OpenOnPhone({ s }: { s: PhoneStatus }) {
  const t = s.tailscale;
  return (
    <Section eyebrow="Open Rectifi on your phone">
      {t.name && s.app_url ? (
        <>
          <p className={p.line}>
            {t.app_published ? <Stamp tone="ok">Published</Stamp> : <Stamp tone="near">Not published yet</Stamp>}
            <span>
              {t.app_published
                ? "On your phone (Tailscale switched on, same account), open this in Chrome:"
                : <>Run <code>python scripts/dev.py tailscale</code> on this laptop once. Then this address works on your phone:</>}
            </span>
          </p>
          <CodeValue value={s.app_url} label="the Rectifi address for your phone" />
          <p className={p.muted}>
            Only devices signed in to your own Tailscale account can open it. To keep it like an app: in Chrome press the {"⋮"} menu, then {"“"}Add to Home screen{"”"}.
          </p>
        </>
      ) : t.installed ? (
        <p className={p.line}>
          <Stamp tone="near">Tailscale is off</Stamp>
          <span>Tailscale is installed on this laptop but not running or not signed in. Open the Tailscale app and sign in, then reload this page.</span>
        </p>
      ) : (
        <p className={p.line}>
          <Stamp tone="near">Not set up</Stamp>
          <span>
            Install <strong>Tailscale</strong> (free) on this laptop and on your phone, sign in to the same account on both, then run <code>python scripts/dev.py tailscale</code>.
            This page then shows the address to open. Full steps: README, {"“"}Use Rectifi on your phone{"”"}.
          </span>
        </p>
      )}
      {s.tailscale_login && <p className={p.muted} data-testid="tailscale-login">Signed in to Tailscale as {s.tailscale_login}.</p>}
    </Section>
  );
}

// ── 2/3. honesty note and the gateway ──

function Honest() {
  return (
    <Callout tone="note" lead="How this works, honestly.">
      Nothing can read SMS from a phone number. Instead, your phone <em>forwards</em> each bank SMS to this laptop, over Tailscale (HTTPS, only your own devices).
      Rectifi reads it with fixed rules (no AI), adds it, and skips duplicates. If the laptop was off, anything missed is caught later by importing a statement and the balance check.
    </Callout>
  );
}

function Gateway({ s }: { s: PhoneStatus }) {
  const { gateway } = s;
  return (
    <Section eyebrow="Gateway">
      {gateway.up ? (
        <>
          <p className={p.line}>
            <Stamp tone="ok">On</Stamp>
            <span>The gateway answers on this laptop at <code>http://127.0.0.1:{gateway.port}/health</code>.</span>
          </p>
          {gateway.open ? (
            <Callout tone="near" lead={`It listens on ${gateway.host}.`}>
              It is open to everyone on the same network, with the device token as the only lock. Remove RECTIFI_SMS_HOST from .env to go back to this laptop only (the default).
            </Callout>
          ) : (
            <p className={p.muted}>
              It listens on this laptop only (127.0.0.1). Your phone reaches it through Tailscale, which only your own devices can use. Switch the gateway off when you do not need it.
            </p>
          )}
        </>
      ) : (
        <>
          <p className={p.line}>
            <Stamp tone="near">Off</Stamp>
            <span>Nothing answers at <code>http://127.0.0.1:{gateway.port}/health</code>. This is the default: the gateway is off until you turn it on.</span>
          </p>
          <p>To turn it on for good, add these lines to your <code>.env</code> file, then restart with <code>python scripts/dev.py up</code>:</p>
          <CodeValue value={`RECTIFI_SMS_GATEWAY=1\nRECTIFI_SMS_PORT=${gateway.port}`} label="the .env lines" wrap />
          <p>Or, just for now, run this in a second terminal:</p>
          <CodeValue value="python scripts/dev.py phone" label="the gateway command" />
          <p className={p.muted}>Then run <code>python scripts/dev.py tailscale</code> so your phone can reach it over HTTPS.</p>
        </>
      )}
    </Section>
  );
}

// ── 4. pairing ──

function download(filename: string, content: string) {
  const blob = new Blob([content], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function PairingPanel({ pair, s, onDone }: { pair: PairResult; s: PhoneStatus; onDone: () => void }) {
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const test = useMutation({
    mutationFn: () => post<{ status: number; said: string }>("/phone/test", { token: pair.token }),
    onSuccess: (r) => {
      if (r.status === 200 && r.said === "ignored") setResult({ ok: true, text: "It works. The gateway accepted the token and answered “ignored”, which is right: a test message is not a transaction, so nothing was stored." });
      else if (r.status === 200) setResult({ ok: true, text: `The gateway answered “${r.said}”.` });
      else setResult({ ok: false, text: `Test failed: ${r.said}.` });
    },
    onError: (e) => setResult({ ok: false, text: e instanceof Error ? e.message : "The test did not run." }),
  });
  const m = pair.manual;
  const secure = m.webhook_url;
  return (
    <div className={p.panel} data-testid="pairing-panel">
      <h3 className={p.panelTitle}>Set up {"“"}{pair.device.name}{"”"}</h3>
      <Callout tone="near" icon={KeyRound} lead="This token is shown once.">
        Rectifi keeps only a fingerprint (a hash) of the token, so it can{"’"}t be shown again. Download the setup file now (or copy the values under {"“"}Set it up by hand{"”"}). Lose it and you pair the phone again.
      </Callout>
      <p>
        <strong>First, on your phone:</strong> install <strong>SMS to URL Forwarder</strong> from F-Droid or IzzyOnDroid (package <code>tech.bogomolov.incomingsmsgateway</code>).
        Then do the steps below <strong>on the phone</strong>: open Rectifi there (the address at the top of this page) and come to this page.
      </p>
      <div>
        <Button
          variant="primary"
          disabled={!pair.setup_file}
          icon={<Download size={16} strokeWidth={1.5} aria-hidden="true" />}
          onClick={() => pair.setup_file && download(pair.setup_file.filename, pair.setup_file.content)}
        >
          Download setup file
        </Button>
      </div>
      {!pair.setup_file && pair.setup_unavailable_reason && <Callout tone="note" role="status">{pair.setup_unavailable_reason} Steps: README, {"“"}Use Rectifi on your phone{"”"}.</Callout>}
      {pair.setup_file && secure && !s.tailscale.gateway_published && (
        <Callout tone="near" lead="The gateway is not published on your tailnet yet.">
          Run <code>python scripts/dev.py tailscale</code> on this laptop (with <code>RECTIFI_SMS_GATEWAY=1</code> in <code>.env</code>), then reload this page.
        </Callout>
      )}
      <ol className={p.steps}>
        <li>Tap <strong>Download setup file</strong> above.</li>
        <li>Open <strong>SMS to URL Forwarder</strong>, tap the {"⋮"} menu (or <strong>Settings</strong>), then <strong>Import</strong>, and choose <code>rectifi-sms-setup.json</code> from <strong>Downloads</strong>.</li>
        <li>Allow the SMS permission when the app asks. In Android{"’"}s battery settings, set the app to <strong>Unrestricted</strong> so the system does not stop it.</li>
        <li>Tap <strong>Send a test message</strong> below.</li>
      </ol>
      <p className={p.muted}>Delete rectifi-sms-setup.json from Downloads afterwards. It contains your key.</p>
      <div className={p.testBox}>
        <p>
          <strong>Test it.</strong> With the gateway on, press the button below. It sends a harmless message from this laptop using the token, so it proves the gateway and the token work.
          It does not prove your phone can reach the laptop: for that, open this address in the phone{"’"}s browser{s.health_url ? ": " : " once you have an address."}
          {s.health_url && <><code>{s.health_url}</code>. It should show <code>{`{"ok": true}`}</code>.</>}
        </p>
        <div className={p.row}>
          <Button
            variant="secondary"
            disabled={!s.gateway.up}
            loading={test.isPending}
            loadingLabel="Sending…"
            icon={<Send size={16} strokeWidth={1.5} aria-hidden="true" />}
            onClick={() => { setResult(null); test.mutate(); }}
          >
            Send a test message
          </Button>
          {!s.gateway.up && <span className={p.muted}>Turn the gateway on first (see the Gateway section).</span>}
        </div>
        {result && <Callout tone={result.ok ? "ok" : "over"} role="status" className={p.result}>{result.text}</Callout>}
      </div>
      <Disclosure summary="Set it up by hand instead">
        <div className={p.manual}>
          <p>In the app, add a forwarding rule with these values:</p>
          {secure ? (
            <>
              <p>Webhook URL ({secure.startsWith("https") ? "HTTPS, through Tailscale: only your own devices can use it" : "plain HTTP on your local network"}):</p>
              <CodeValue value={secure} label="the webhook URL" wrap />
            </>
          ) : (
            <Callout tone="note">The gateway listens on this laptop only, so your phone needs Tailscale to reach it. Install Tailscale on both devices (same account), run <code>python scripts/dev.py tailscale</code>, and the HTTPS address appears here.</Callout>
          )}
          {s.gateway.open && (
            <Callout tone="near" lead="Advanced: the gateway is open.">
              RECTIFI_SMS_HOST is {s.gateway.host}, so the gateway is open to everyone on the same network and the token is the only lock. Plain HTTP addresses below. Prefer the default (this laptop only) with Tailscale.
            </Callout>
          )}
          {s.gateway.open && s.lan_addresses.length > 0 && (
            <>
              <p>Local network addresses (the phone must be on the same Wi-Fi; if the first does not work, try the next):</p>
              {s.lan_addresses.map((a) => (
                <div key={a.ip}>
                  <CodeValue value={`http://${a.ip}:${s.gateway.port}/sms`} label={`the address on ${a.ip}`} wrap />
                  <p className={p.muted}>{a.label}</p>
                </div>
              ))}
            </>
          )}
          <p>Custom header (the app asks for headers as JSON):</p>
          <CodeValue value={m.headers_json} label="the Authorization header" wrap />
          <p>JSON body template:</p>
          <CodeValue value={m.template} label="the JSON body template" wrap />
          <p>Sender filter (regex). Indian bank SMS come from IDs like <code>AX-HDFCBK</code> or <code>VM-SBIUPI</code>. This may need adjusting for your bank (the setup file accepts any sender and relies on the text filter):</p>
          <CodeValue value={m.sender_regex} label="the sender filter" wrap />
          <p>Text filter (regex):</p>
          <CodeValue value={m.text_regex} label="the text filter" wrap />
          <p>If the app has a {"“"}Local network mode{"”"} option, tick it: it is for addresses on your own network. Leave retries on (the app tries again for a while if the laptop is off).</p>
        </div>
      </Disclosure>
      <Disclosure summary="iPhone (Shortcuts): untested">
        <div className={p.manual}>
          <Callout tone="near" icon={FlaskConical}>These iPhone steps are written from how Shortcuts works. Nobody has run them against Rectifi yet, so expect to adjust them.</Callout>
          <ol className={p.steps}>
            <li>Open <strong>Shortcuts</strong>, go to <strong>Automation</strong>, tap <strong>+</strong>, choose <strong>Message</strong>.</li>
            <li>Set <strong>Message Contains</strong> to a word such as <code>debited</code>. Make one automation per word: <code>debited</code>, <code>credited</code>.</li>
            <li>Choose <strong>Run Immediately</strong> (not {"“"}Run after confirmation{"”"}).</li>
            <li>Add the action <strong>Get Contents of URL</strong>. Set the URL to the first address above.</li>
            <li>Open <strong>Show More</strong>: Method <strong>POST</strong>, add a header <code>Authorization</code> with the value <code>Bearer &lt;your token&gt;</code>, set Request Body to <strong>JSON</strong>, and add two fields: <code>text</code> = the <strong>Shortcut Input</strong> (the message), <code>from</code> = <strong>Sender</strong> of the message.</li>
            <li>Save. Send yourself a test message to check.</li>
          </ol>
        </div>
      </Disclosure>
      <div>
        <Button variant="secondary" icon={<EyeOff size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={onDone}>Done: hide the token</Button>
      </div>
    </div>
  );
}

function Pair({ s }: { s: PhoneStatus }) {
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The token lives in this component's state only. It is not put in the query cache, the URL or storage.
  const [pair, setPair] = useState<PairResult | null>(null);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const made = await post<PairResult>("/phone/devices", { name: name.trim() });
      setPair(made);
      setName("");
      void client.invalidateQueries({ queryKey: keys.phoneDevices });
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Could not create a token.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section eyebrow="Pair a phone">
      <form className={p.pairForm} onSubmit={(e) => { e.preventDefault(); void create(); }} noValidate aria-label="Pair a phone">
        <Field label="Phone name" error={error}>
          {(f) => <TextInput {...f} value={name} placeholder="My Android" maxLength={NAME_MAX} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Button type="submit" variant="secondary" icon={<Link2 size={16} strokeWidth={1.5} aria-hidden="true" />} loading={busy} loadingLabel="Creating…">Create a pairing token</Button>
      </form>
      {pair && <PairingPanel pair={pair} s={s} onDone={() => setPair(null)} />}
    </Section>
  );
}

// ── 5. paired phones ──

function DeviceRow({ d, onRevoke }: { d: Device; onRevoke: (d: Device) => void }) {
  const revoked = d.revoked_at !== null;
  return (
    <li className={p.device} data-testid="device-row">
      <div className={p.deviceMain}>
        <span className={p.deviceName}>{d.name}</span>
        {revoked ? <Stamp tone="over">Revoked</Stamp> : <Stamp tone="ok">Active</Stamp>}
      </div>
      <p className={p.muted}>Last message: {d.last_seen_at ? `${timeAgo(d.last_seen_at)} (${fmtDateTime(d.last_seen_at)})` : "never"}</p>
      {!revoked && <Button variant="tertiary" icon={<Ban size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => onRevoke(d)}>Revoke</Button>}
    </li>
  );
}

function Devices() {
  const query = usePhoneDevices();
  const client = useQueryClient();
  const toast = useToast();
  const [target, setTarget] = useState<Device | null>(null);
  const revoke = useMutation({
    mutationFn: (d: Device) => post<{ revoked: boolean }>(`/phone/devices/${d.id}/revoke`),
    onSuccess: (_r, d) => {
      toast(`${d.name} can no longer send messages.`);
      setTarget(null);
      void client.invalidateQueries({ queryKey: keys.phoneDevices });
    },
    onError: (e) => { toast(e instanceof Error ? e.message : "Could not revoke that phone.", "over"); setTarget(null); },
  });
  return (
    <Section eyebrow="Paired phones">
      <Loaded query={query} skeleton={<Skeleton height={64} />}>
        {(items) =>
          items.length === 0 ? (
            <EmptyState title="No phone paired yet.">Create a pairing token above.</EmptyState>
          ) : (
            <ul className={p.devices}>{items.map((d) => <DeviceRow key={d.id} d={d} onRevoke={setTarget} />)}</ul>
          )
        }
      </Loaded>
      <ConfirmDialog
        open={target !== null}
        title="Revoke this phone?"
        confirmLabel="Revoke"
        busyLabel="Revoking…"
        busy={revoke.isPending}
        onConfirm={() => target && revoke.mutate(target)}
        onClose={() => setTarget(null)}
      >
        {target ? `${target.name}'s token stops working at once. To use it again you pair the phone again.` : ""}
      </ConfirmDialog>
    </Section>
  );
}

// ── 6. needs review ──

function Inbox() {
  const query = usePhoneInbox();
  const client = useQueryClient();
  const toast = useToast();
  const dismiss = useMutation({
    mutationFn: (i: InboxItem) => post<{ dismissed: boolean }>(`/phone/inbox/${i.id}/dismiss`),
    onSuccess: () => { toast("Dismissed."); void client.invalidateQueries({ queryKey: keys.phoneInbox }); },
    onError: (e) => toast(e instanceof Error ? e.message : "Could not dismiss that.", "over"),
  });
  const n = query.data?.length;
  return (
    <Section eyebrow={`Needs review${n !== undefined ? ` (${n})` : ""}`}>
      <Loaded query={query} skeleton={<Skeleton height={90} />}>
        {(items) =>
          items.length === 0 ? (
            <EmptyState title="Nothing waiting.">A bank-looking SMS that Rectifi{"’"}s fixed rules can{"’"}t read for sure waits here for you. OTPs and offers are thrown away, not kept.</EmptyState>
          ) : (
            <ul className={p.inbox}>
              {items.map((i) => (
                <li key={i.id} className={p.inboxItem} data-testid="inbox-item">
                  <pre className={p.raw}>{i.raw_text}</pre>
                  <p className={p.muted}>
                    From {i.sender ?? "unknown"} via {i.device ?? "a removed phone"} {"·"} {fmtDateTime(i.sent_at ?? i.received_at)}
                    {i.reason ? ` · ${i.reason}` : ""}
                  </p>
                  <div className={p.row}>
                    <LinkButton to={`/add?inbox=${i.id}`} variant="secondary" icon={<NotebookPen size={16} strokeWidth={1.5} aria-hidden="true" />}>Add</LinkButton>
                    <Button variant="tertiary" icon={<X size={16} strokeWidth={1.5} aria-hidden="true" />} loading={dismiss.isPending && dismiss.variables?.id === i.id} loadingLabel="Dismissing…" onClick={() => dismiss.mutate(i)}>Dismiss</Button>
                  </div>
                </li>
              ))}
            </ul>
          )
        }
      </Loaded>
    </Section>
  );
}

// ── 7. good to know ──

function GoodToKnow() {
  return (
    <Section eyebrow="Good to know">
      <ul className={p.know}>
        <li>The laptop must be on and Rectifi running, or the phone can{"’"}t deliver. The Android app keeps retrying for a while. Anything still missed is caught when you import a statement, and the balance check shows the gap.</li>
        <li><strong>Campus Wi-Fi often blocks one device from reaching another</strong> (client isolation), which is why Rectifi uses <strong>Tailscale</strong>: both devices connect out to Tailscale, so it works on any network, hostel Wi-Fi included. No router settings, no open ports.</li>
        <li>Another way, if you cannot use Tailscale: turn on the phone{"’"}s hotspot and connect the laptop to it, then set <code>RECTIFI_SMS_HOST=0.0.0.0</code> in <code>.env</code> (advanced: it opens the gateway to that network, and Windows Firewall asks the first time; allow it on <strong>Private networks only</strong>).</li>
        <li>Offers, OTPs and chats are ignored and never stored. An SMS from a plain phone number is ignored too.</li>
        <li>If you lose a phone, press Revoke. Its token stops working immediately.</li>
      </ul>
    </Section>
  );
}

export default function PhonePage() {
  const status = usePhoneStatus();
  return (
    <>
      <PageHeader name="Phone" title="Phone" />
      <Loaded query={status} skeleton={<div className={styles.skelStack} aria-hidden="true"><Skeleton height={90} /><Skeleton height={120} /></div>}>
        {(s) => (
          <div className={p.page}>
            <OpenOnPhone s={s} />
            <Honest />
            <Gateway s={s} />
            <Pair s={s} />
            <Devices />
            <Inbox />
            <GoodToKnow />
          </div>
        )}
      </Loaded>
    </>
  );
}

