import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "wouter";
import { navigate } from "wouter/use-browser-location";
import { changePassword, deleteAccount, exportAccount, newRecoveryCode } from "../api/account";
import { hasAccount, useAuthStatus, useSignOutEverywhere } from "../api/auth";
import { fieldProblem, formProblem, problemOf, useSecretCall } from "../api/secret";
import { Button, Field, LinkButton, TextInput } from "../components/forms";
import { PageHeader } from "../components/PageHeader";
import { PasswordField } from "../components/PasswordField";
import { RecoveryGate } from "../components/RecoveryGate";
import { Callout, EmptyState, InlineStatus, Section, Skeleton, Stamp } from "../components/ui";
import { MIN_PASSWORD, saveBlob } from "../lib/recovery";
import s from "./settings.module.css";

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const [done, setDone] = useState(false);
  const call = useSecretCall(changePassword);
  const problem = problemOf(call.error);
  const FIELDS = ["current", "new"];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (call.pending) return;
    setDone(false);
    if (next !== again) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    const out = await call.run({ current, new: next });
    if (out) {
      setCurrent("");
      setNext("");
      setAgain("");
      setDone(true);
    }
  }
  return (
    <section className={s.panel} aria-labelledby="pw-h">
      <h2 id="pw-h" className={s.panelTitle}>Change password</h2>
      <p className={s.muted}>Other devices are signed out when you change it. Your sealed text stays readable. Your recovery code stays the same.</p>
      <form className={s.form} onSubmit={(e) => void submit(e)} noValidate>
        {formProblem(problem, FIELDS) && <Callout tone="near" role="alert">{formProblem(problem, FIELDS)}</Callout>}
        <PasswordField label="Current password" name="current-password" autoComplete="current-password" error={fieldProblem(problem, "current")}
          value={current} onChange={(v) => { setCurrent(v); call.reset(); setDone(false); }} />
        <PasswordField label="New password" name="new-password" autoComplete="new-password" hint={`At least ${MIN_PASSWORD} characters.`}
          error={fieldProblem(problem, "new")} value={next} onChange={(v) => { setNext(v); setMismatch(false); call.reset(); setDone(false); }} />
        <PasswordField label="Repeat new password" name="repeat-password" autoComplete="new-password" error={mismatch ? "The two passwords are not the same." : null}
          value={again} onChange={(v) => { setAgain(v); setMismatch(false); }} />
        <div className={s.row}>
          <Button type="submit" variant="primary" loading={call.pending} loadingLabel="Saving…" disabled={!current || !next || !again}>Change password</Button>
          {done && <InlineStatus kind="saved">Password changed.</InlineStatus>}
        </div>
      </form>
    </section>
  );
}

function NewCode({ username }: { username: string }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const call = useSecretCall(newRecoveryCode);
  const problem = problemOf(call.error);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (call.pending || !password) return;
    const out = await call.run(password);
    if (out) {
      setCode(out.recovery_code);
      setPassword("");
    }
  }
  return (
    <section className={s.panel} aria-labelledby="rc-h">
      <h2 id="rc-h" className={s.panelTitle}>New recovery code</h2>
      {code === null ? (
        <>
          <p className={s.muted}>Lost or shared your code? Make a new one. The old code stops working at once.</p>
          <form className={s.form} onSubmit={(e) => void submit(e)} noValidate>
            {problem && <Callout tone="near" role="alert">{problem.message}</Callout>}
            <PasswordField label="Your password" name="current-password" autoComplete="current-password"
              value={password} onChange={(v) => { setPassword(v); call.reset(); }} />
            <div><Button type="submit" variant="secondary" loading={call.pending} loadingLabel="Making it…" disabled={!password}>Make a new recovery code</Button></div>
          </form>
        </>
      ) : (
        <RecoveryGate
          code={code}
          username={username}
          finishLabel="Done"
          finishing={false}
          finishingLabel="Done"
          onFinish={() => setCode(null)}
          intro={<p>The old code no longer works. Use this one from now on.</p>}
        />
      )}
    </section>
  );
}

function Export() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  async function run() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const { blob, filename } = await exportAccount();
      saveBlob(blob, filename ?? "rectifi-export.zip");
      setSaved(true);
    } catch (e) {
      setError(problemOf(e)?.message ?? "The export did not work. Try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className={s.panel} aria-labelledby="ex-h">
      <h2 id="ex-h" className={s.panelTitle}>Export your data</h2>
      <p className={s.muted}>A ZIP of CSV files with your accounts, entries, budgets and alerts. Amounts are text, so they stay exact. Open it in Excel with Data, From Text/CSV, UTF-8.</p>
      {error && <Callout tone="near" role="alert">{error}</Callout>}
      <div className={s.row}>
        <Button variant="secondary" loading={busy} loadingLabel="Preparing…" onClick={() => void run()}>Download my data (.zip)</Button>
        {saved && <InlineStatus kind="saved">Downloaded.</InlineStatus>}
      </div>
    </section>
  );
}

function DeleteAccount({ username }: { username: string }) {
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [typed, setTyped] = useState("");
  const call = useSecretCall(deleteAccount);
  const problem = problemOf(call.error);
  const FIELDS = ["password", "confirm_username"];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (call.pending || !password || !typed.trim()) return;
    const out = await call.run({ password, confirm_username: typed.trim() });
    if (out) {
      setPassword("");
      setTyped("");
      qc.clear();
      navigate("/login", { replace: true });
    }
  }
  return (
    <section className={`${s.panel} ${s.danger}`} aria-labelledby="del-h">
      <h2 id="del-h" className={s.panelTitle}>Delete my account</h2>
      <Callout tone="over" lead="This cannot be undone, and there is no reset.">
        Every entry, account, budget and alert you own is deleted for good. Backups can keep old sealed copies for a few days. Download your data first if you want it.
      </Callout>
      <form className={s.form} onSubmit={(e) => void submit(e)} noValidate>
        {formProblem(problem, FIELDS) && <Callout tone="near" role="alert">{formProblem(problem, FIELDS)}</Callout>}
        <PasswordField label="Your password" name="current-password" autoComplete="current-password" error={fieldProblem(problem, "password")}
          value={password} onChange={(v) => { setPassword(v); call.reset(); }} />
        <Field label={`Type your username (${username}) to confirm`} error={fieldProblem(problem, "confirm_username")}>
          {(field) => (
            <TextInput {...field} name="confirm-username" autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={typed}
              onChange={(e) => { setTyped(e.target.value); call.reset(); }} />
          )}
        </Field>
        <div>
          <Button type="submit" variant="danger" loading={call.pending} loadingLabel="Deleting…"
            disabled={!password || typed.trim().toLowerCase() !== username.toLowerCase()}>
            Delete my account for good
          </Button>
        </div>
      </form>
    </section>
  );
}

function Devices() {
  const out = useSignOutEverywhere();
  return (
    <section className={s.panel} aria-labelledby="dev-h">
      <h2 id="dev-h" className={s.panelTitle}>Devices</h2>
      <p className={s.muted}>Lost a phone or used a shared computer? Sign out everywhere.</p>
      <div><Button variant="secondary" loading={out.isPending} loadingLabel="Signing out…" onClick={() => out.mutate()}>Sign out on every device</Button></div>
    </section>
  );
}

export default function SettingsPage() {
  const status = useAuthStatus();
  const data = status.data;
  return (
    <>
      <PageHeader name="Settings" title="Your account" sub="Password, recovery code, export and delete." />
      {!data ? (
        <Skeleton height={160} />
      ) : !hasAccount(data) || !data.username ? (
        <EmptyState title="There are no accounts on this Rectifi." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
          Settings are for a Rectifi where people sign up with an invite. This one has one person.
        </EmptyState>
      ) : (
        <div className={s.page}>
          <Section eyebrow="Account">
            <div className={s.panel}>
              <div className={s.who}>
                <span>Signed in as</span>
                <strong data-testid="settings-username">{data.username}</strong>
                <Stamp tone={data.is_owner ? "accent" : "neutral"}>{data.is_owner ? "Owner" : "Friend"}</Stamp>
              </div>
              <div className={s.links}>
                <Link href="/privacy">How your data is protected</Link>
              </div>
            </div>
          </Section>
          <ChangePassword />
          <NewCode username={data.username} />
          <Devices />
          <Export />
          <DeleteAccount username={data.username} />
        </div>
      )}
    </>
  );
}
