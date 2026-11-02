import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useLocation } from "wouter";
import { recoverAccount } from "../api/account";
import { useAuthStatus } from "../api/auth";
import { fieldProblem, formProblem, problemOf, useSecretCall } from "../api/secret";
import { AuthScreen, authStyles as styles } from "../components/AuthScreen";
import { Button, Field, TextInput } from "../components/forms";
import { PasswordField } from "../components/PasswordField";
import { RecoveryGate } from "../components/RecoveryGate";
import { Callout } from "../components/ui";
import { MIN_PASSWORD } from "../lib/recovery";

export default function RecoverPage() {
  const [, navigate] = useLocation();
  const qc = useQueryClient();
  const status = useAuthStatus();
  const [username, setUsername] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const [fresh, setFresh] = useState<{ code: string; username: string } | null>(null);
  const recover = useSecretCall(recoverAccount);

  const problem = problemOf(recover.error);
  const FIELDS = ["username", "recovery_code", "new_password"];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (recover.pending) return;
    if (password !== again) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    const name = username.trim();
    const out = await recover.run({ username: name, recovery_code: code.trim(), new_password: password });
    if (out) {
      setFresh({ code: out.recovery_code, username: name });
      setCode("");
      setPassword("");
      setAgain("");
    }
  }

  function finish() {
    setFresh(null); // the new code leaves memory here
    qc.clear();
    navigate("/login", { replace: true });
  }

  if (status.data && !status.data.multiuser) {
    return (
      <AuthScreen eyebrow="Recover" title="Recovery is off here">
        <p>This Rectifi has one shared password and no accounts. To change it, run <code>python scripts/dev.py set-password</code> on the server.</p>
        <nav className={styles.links} aria-label="Other pages"><Link href="/login">Sign in</Link></nav>
      </AuthScreen>
    );
  }

  if (fresh) {
    return (
      <AuthScreen eyebrow="Recover · done" title="Your new recovery code">
        <RecoveryGate
          code={fresh.code}
          username={fresh.username}
          finishLabel="Go to sign in"
          finishing={false}
          finishingLabel="Going…"
          onFinish={finish}
          intro={<p>Your password is changed and you are signed out everywhere. The old code no longer works. This is the new one.</p>}
        />
      </AuthScreen>
    );
  }

  return (
    <AuthScreen eyebrow="Recover" title="Get back in">
      <form className={styles.form} onSubmit={(e) => void submit(e)} noValidate>
        {formProblem(problem, FIELDS) && <Callout tone="near" role="alert">{formProblem(problem, FIELDS)}</Callout>}
        <Field label="Username" error={fieldProblem(problem, "username")}>
          {(field) => (
            <TextInput {...field} name="username" autoComplete="username" autoCapitalize="off" autoCorrect="off" spellCheck={false} autoFocus value={username}
              onChange={(e) => { setUsername(e.target.value); recover.reset(); }} />
          )}
        </Field>
        <Field label="Recovery code" hint="The code you saved when you signed up. Each code works once." error={fieldProblem(problem, "recovery_code")}>
          {(field) => (
            <TextInput {...field} name="recovery-code" autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={code}
              onChange={(e) => { setCode(e.target.value); recover.reset(); }} />
          )}
        </Field>
        <PasswordField label="New password" name="new-password" autoComplete="new-password" hint={`At least ${MIN_PASSWORD} characters.`}
          error={fieldProblem(problem, "new_password")} value={password} onChange={(v) => { setPassword(v); setMismatch(false); recover.reset(); }} />
        <PasswordField label="Repeat new password" name="repeat-password" autoComplete="new-password"
          error={mismatch ? "The two passwords are not the same." : null} value={again} onChange={(v) => { setAgain(v); setMismatch(false); }} />
        <Button type="submit" variant="primary" block loading={recover.pending} loadingLabel="Checking…"
          disabled={!username.trim() || !code.trim() || !password || !again || status.isPending}>
          Set a new password
        </Button>
      </form>
      <nav className={styles.links} aria-label="Other pages">
        <Link href="/login">Back to sign in</Link>
        <Link href="/privacy">How your data is protected</Link>
      </nav>
      <p className={styles.help}>No code and no password? There is no reset. Nobody can recover your sealed text for you.</p>
    </AuthScreen>
  );
}
