import { useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Link } from "wouter";
import { navigate } from "wouter/use-browser-location";
import { changePassword, deleteAccount, exportAccount, newRecoveryCode } from "../api/account";
import { hasAccount, useAuthStatus } from "../api/auth";
import { fieldProblem, problemOf, useSecretCall } from "../api/secret";
import { Button, LinkButton } from "../components/forms";
import { NewPasswordFields, useNewPassword } from "../components/NewPasswordFields";
import { PageHeader } from "../components/PageHeader";
import { PasswordField } from "../components/PasswordField";
import { RecoveryGate } from "../components/RecoveryGate";
import { SecretForm } from "../components/SecretForm";
import { TextBox } from "../components/TextBox";
import { Callout, EmptyState, InlineStatus, Skeleton } from "../components/ui";
import { saveBlob } from "../lib/recovery";
import s from "./settings.module.css";

function Panel({ id, title, danger, children }: { id: string; title: string; danger?: boolean; children: ReactNode }) {
  return (
    <section className={danger ? `${s.panel} ${s.danger}` : s.panel} aria-labelledby={id}>
      <h2 id={id} className={s.panelTitle}>{title}</h2>
      {children}
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const pw = useNewPassword();
  const [done, setDone] = useState(false);
  const call = useSecretCall(changePassword);
  const problem = problemOf(call.error);
  const edit = () => { call.reset(); setDone(false); };

  async function submit() {
    if (call.pending) return;
    setDone(false);
    if (!pw.check()) return;
    if (await call.run({ current, new: pw.password })) {
      setCurrent("");
      pw.clear();
      setDone(true);
    }
  }
  return (
    <Panel id="pw-h" title="Change password">
      <SecretForm className={s.form} problem={problem} fields={["current", "new"]} onSubmit={submit}>
        <PasswordField label="Current password" name="current-password" autoComplete="current-password" error={fieldProblem(problem, "current")}
          value={current} onChange={(v) => { setCurrent(v); edit(); }} />
        <NewPasswordFields label="New password" pw={pw} error={fieldProblem(problem, "new")} onEdit={edit} />
        <div className={s.row}>
          <Button type="submit" variant="primary" loading={call.pending} loadingLabel="Saving…" disabled={!current || !pw.filled}>Change password</Button>
          {done && <InlineStatus kind="saved">Password changed.</InlineStatus>}
        </div>
      </SecretForm>
    </Panel>
  );
}

function NewCode({ username }: { username: string }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const call = useSecretCall(newRecoveryCode);
  const problem = problemOf(call.error);

  async function submit() {
    const out = password && !call.pending ? await call.run(password) : undefined;
    if (out) {
      setCode(out.recovery_code);
      setPassword("");
    }
  }
  return (
    <Panel id="rc-h" title="New recovery code">
      {code === null ? (
        <SecretForm className={s.form} problem={problem} fields={[]} onSubmit={submit}>
          <PasswordField label="Your password" name="current-password" autoComplete="current-password" value={password} onChange={(v) => { setPassword(v); call.reset(); }} />
          <div><Button type="submit" variant="secondary" loading={call.pending} loadingLabel="Making it…" disabled={!password}>Make a new recovery code</Button></div>
        </SecretForm>
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
    </Panel>
  );
}

function Export() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run() {
    setBusy(true);
    setError(null);
    try {
      const { blob, filename } = await exportAccount();
      saveBlob(blob, filename ?? "rectifi-export.zip");
    } catch (e) {
      setError(problemOf(e)?.message ?? "The export did not work. Try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Panel id="ex-h" title="Export your data">
      {error && <Callout tone="near" role="alert">{error}</Callout>}
      <div><Button variant="secondary" loading={busy} loadingLabel="Preparing…" onClick={() => void run()}>Download my data (.zip)</Button></div>
    </Panel>
  );
}

const OWNER_DELETE = "The owner account can only be deleted on the server: stop Rectifi, then run python scripts/dev.py reset-owner.";

function OwnerDelete() {
  return (
    <Panel id="del-h" title="Delete my account">
      <p data-testid="owner-delete-note">{OWNER_DELETE}</p>
      <p>Backups can keep copies for a few days. Your chat, memories, charts, library and traces are not sealed, so a backup keeps them readable.</p>
    </Panel>
  );
}

function DeleteAccount({ username }: { username: string }) {
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [typed, setTyped] = useState("");
  const call = useSecretCall(deleteAccount);
  const problem = problemOf(call.error);

  async function submit() {
    if (call.pending || !password || !typed.trim()) return;
    if (await call.run({ password, confirm_username: typed.trim() })) {
      setPassword("");
      setTyped("");
      qc.clear();
      navigate("/login", { replace: true });
    }
  }
  return (
    <Panel id="del-h" title="Delete my account" danger>
      <Callout tone="over" lead="This cannot be undone, and there is no reset.">
        Everything you own is deleted. Backups can keep copies for a few days: sealed text stays sealed, but amounts and dates are readable.
      </Callout>
      <SecretForm className={s.form} problem={problem} fields={["password", "confirm_username"]} onSubmit={submit}>
        <PasswordField label="Your password" name="current-password" autoComplete="current-password" error={fieldProblem(problem, "password")}
          value={password} onChange={(v) => { setPassword(v); call.reset(); }} />
        <TextBox label={`Type your username (${username}) to confirm`} name="confirm-username" error={fieldProblem(problem, "confirm_username")}
          value={typed} onChange={(v) => { setTyped(v); call.reset(); }} />
        <div>
          <Button type="submit" variant="danger" loading={call.pending} loadingLabel="Deleting…"
            disabled={!password || typed.trim().toLowerCase() !== username.toLowerCase()}>
            Delete my account for good
          </Button>
        </div>
      </SecretForm>
    </Panel>
  );
}

export default function SettingsPage() {
  const data = useAuthStatus().data;
  return (
    <>
      <PageHeader name="Settings" title="Your account" sub={data?.username && <>Signed in as <strong data-testid="settings-username">{data.username}</strong> ({data.is_owner ? "owner" : "friend"})</>} />
      {!data ? (
        <Skeleton height={160} />
      ) : !hasAccount(data) || !data.username ? (
        <EmptyState title="There are no accounts on this Rectifi." actions={<LinkButton to="/" variant="primary">Go to Overview</LinkButton>}>
          Settings are for a Rectifi where people sign up with an invite.
        </EmptyState>
      ) : (
        <div className={s.page}>
          <Link href="/privacy" className={s.link}>How your data is protected</Link>
          <ChangePassword />
          <NewCode username={data.username} />
          <Export />
          {data.is_owner ? <OwnerDelete /> : <DeleteAccount username={data.username} />}
        </div>
      )}
    </>
  );
}
