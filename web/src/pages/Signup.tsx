import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useLocation } from "wouter";
import { confirmSignup, startSignup } from "../api/account";
import { useAuthStatus } from "../api/auth";
import { fieldProblem, formProblem, problemOf, useSecretCall } from "../api/secret";
import { AuthScreen, authStyles as styles } from "../components/AuthScreen";
import { Button, Field, TextInput } from "../components/forms";
import { PasswordField } from "../components/PasswordField";
import { RecoveryGate } from "../components/RecoveryGate";
import { Callout } from "../components/ui";
import { MIN_PASSWORD } from "../lib/recovery";

/** Held in component state only: the token and the code are never put in the URL, storage or the query cache. */
interface Pending { token: string; code: string; username: string }


export default function SignupPage() {
  const [, navigate] = useLocation();
  const qc = useQueryClient();
  const status = useAuthStatus();
  const [invite, setInvite] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [lost, setLost] = useState<string | null>(null);

  const start = useSecretCall(startSignup);
  const confirm = useSecretCall(confirmSignup);

  const off = status.data ? !status.data.multiuser : false;
  const already = status.data?.signed_in === true && status.data.required && !pending;
  useEffect(() => {
    if (already) navigate("/", { replace: true });
  }, [already, navigate]);

  const problem = problemOf(start.error);
  const FIELDS = ["invite", "username", "password"];

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (start.pending) return;
    setLost(null);
    if (password !== again) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    const name = username.trim();
    const out = await start.run({ invite: invite.trim(), username: name, password });
    if (out) {
      setPending({ token: out.signup_token, code: out.recovery_code, username: name });
      setPassword("");
      setAgain("");
      setInvite("");
    }
  }

  async function finish() {
    if (!pending) return;
    const out = await confirm.run(pending.token);
    if (!out) return;
    setPending(null); // drop the code and the token before leaving the page
    qc.clear();
    navigate("/", { replace: true });
  }

  const confirmProblem = problemOf(confirm.error);
  const resetConfirm = confirm.reset;
  // A spent or old token means the account was never made. Say so and go back to the form.
  useEffect(() => {
    if (confirmProblem?.code === "signup_expired" && pending) {
      setPending(null);
      setLost("That sign-up took too long and was cancelled. Nothing was created. Fill in the form again to start over.");
      resetConfirm();
    }
  }, [confirmProblem?.code, pending, resetConfirm]);

  if (off) {
    return (
      <AuthScreen eyebrow="Sign up" title="Sign-up is off here">
        <p>This Rectifi has one person and no accounts, so there is nothing to sign up for.</p>
        <nav className={styles.links} aria-label="Other pages"><Link href="/login">Sign in</Link></nav>
      </AuthScreen>
    );
  }

  if (pending) {
    return (
      <AuthScreen eyebrow="Sign up · step 2 of 2" title="Save your recovery code">
        <RecoveryGate
          code={pending.code}
          username={pending.username}
          finishLabel="Finish sign-up"
          finishing={confirm.pending}
          finishingLabel="Finishing…"
          onFinish={() => void finish()}
          error={confirmProblem && confirmProblem.code !== "signup_expired" ? confirmProblem.message : null}
          intro={<p>Your account is not made yet. It is made when you press Finish. If you leave this page first, nothing is created.</p>}
        />
      </AuthScreen>
    );
  }

  return (
    <AuthScreen eyebrow="Sign up · step 1 of 2" title="Create your account">
      <form className={styles.form} onSubmit={(e) => void submit(e)} noValidate>
        {lost && <Callout tone="near" role="alert">{lost}</Callout>}
        {formProblem(problem, FIELDS) && <Callout tone="near" role="alert">{formProblem(problem, FIELDS)}</Callout>}
        <Field label="Invite code" hint="The code you were given. It works once." error={fieldProblem(problem, "invite")}>
          {(field) => (
            <TextInput {...field} name="invite" autoComplete="off" autoCapitalize="off" autoCorrect="off" spellCheck={false} autoFocus value={invite}
              onChange={(e) => { setInvite(e.target.value); start.reset(); }} />
          )}
        </Field>
        <Field label="Username" hint="3 to 32 letters, digits, dots, dashes or underscores. No email." error={fieldProblem(problem, "username")}>
          {(field) => (
            <TextInput {...field} name="username" autoComplete="username" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={username}
              onChange={(e) => { setUsername(e.target.value); start.reset(); }} />
          )}
        </Field>
        <PasswordField label="Password" name="new-password" autoComplete="new-password" hint={`At least ${MIN_PASSWORD} characters. A few words in a row works well.`}
          error={fieldProblem(problem, "password")} value={password} onChange={(v) => { setPassword(v); setMismatch(false); start.reset(); }} />
        <PasswordField label="Repeat password" name="repeat-password" autoComplete="new-password"
          error={mismatch ? "The two passwords are not the same." : null} value={again} onChange={(v) => { setAgain(v); setMismatch(false); }} />
        <Callout tone="note">There is no password reset. You get a recovery code on the next screen. Keep it.</Callout>
        <Button type="submit" variant="primary" block loading={start.pending} loadingLabel="Checking…"
          disabled={!invite.trim() || !username.trim() || !password || !again || status.isPending}>
          Continue
        </Button>
      </form>
      <nav className={styles.links} aria-label="Other pages">
        <Link href="/login">I already have an account</Link>
        <Link href="/privacy">How your data is protected</Link>
      </nav>
    </AuthScreen>
  );
}
