import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation } from "wouter";
import { confirmSignup, recoverAccount, startSignup } from "../api/account";
import { useAuthStatus } from "../api/auth";
import { fieldProblem, problemOf, useSecretCall } from "../api/secret";
import { AuthScreen, authStyles as styles } from "./AuthScreen";
import { Button } from "./forms";
import { NewPasswordFields, useNewPassword } from "./NewPasswordFields";
import { RecoveryGate } from "./RecoveryGate";
import { SecretForm } from "./SecretForm";
import { TextBox } from "./TextBox";

/** The two doors that end at a recovery code: sign up with an invite, or get back in with the old code.
 *  The token, the code and the passwords are held in this component's state only: never in the URL, storage or the query cache. */
export function AccountEntry({ mode }: { mode: "signup" | "recover" }) {
  const up = mode === "signup";
  const [, navigate] = useLocation();
  const qc = useQueryClient();
  const status = useAuthStatus();
  const [first, setFirst] = useState(""); // the invite code, or the old recovery code
  const [username, setUsername] = useState("");
  const pw = useNewPassword();
  const [held, setHeld] = useState<{ token?: string; code: string; username: string } | null>(null);

  const start = useSecretCall(async (i: { name: string; first: string; password: string }) =>
    up
      ? startSignup({ invite: i.first, username: i.name, password: i.password }).then((r) => ({ token: r.signup_token, code: r.recovery_code }))
      : recoverAccount({ username: i.name, recovery_code: i.first, new_password: i.password }).then((r) => ({ token: undefined, code: r.recovery_code })));
  const confirm = useSecretCall(confirmSignup);

  const already = up && status.data?.signed_in === true && status.data.required && !held;
  useEffect(() => {
    if (already) navigate("/", { replace: true });
  }, [already, navigate]);

  const problem = problemOf(start.error);

  async function submit() {
    if (start.pending || !pw.check()) return;
    const name = username.trim();
    const out = await start.run({ name, first: first.trim(), password: pw.password });
    if (out) {
      setHeld({ ...out, username: name });
      pw.clear();
      setFirst("");
    }
  }

  async function finish() {
    if (!held || (held.token && !(await confirm.run(held.token)))) return;
    setHeld(null); // the code and the token leave memory before the page does
    qc.clear();
    navigate(up ? "/" : "/login", { replace: true });
  }

  if (held) {
    return (
      <AuthScreen eyebrow={up ? "Sign up" : "Recover"} title={up ? "Save your recovery code" : "Your new recovery code"}>
        <RecoveryGate
          code={held.code}
          username={held.username}
          finishLabel={up ? "Finish sign-up" : "Go to sign in"}
          finishing={confirm.pending}
          finishingLabel="Finishing…"
          onFinish={() => void finish()}
          error={problemOf(confirm.error)?.message}
          intro={up
            ? <><p>Your account is made when you press Finish. If you leave this page first, nothing is created.</p><div className={styles.links}><a href="/signup">Start again</a></div></>
            : <p>Your password is changed and you are signed out everywhere. The old code no longer works. This is the new one.</p>}
        />
      </AuthScreen>
    );
  }

  return (
    <AuthScreen eyebrow={up ? "Sign up" : "Recover"} title={up ? "Create your account" : "Get back in"}>
      <SecretForm className={styles.form} problem={problem} fields={up ? ["invite", "username", "password"] : ["username", "recovery_code", "new_password"]} onSubmit={submit}>
        <TextBox label={up ? "Invite code" : "Recovery code"} name="code" error={fieldProblem(problem, up ? "invite" : "recovery_code")} autoFocus
          hint={up ? "It works once." : "The code you saved at sign-up. It works once."}
          value={first} onChange={(v) => { setFirst(v); start.reset(); }} />
        <TextBox label="Username" name="username" autoComplete="username" error={fieldProblem(problem, "username")}
          hint={up ? "3 to 32 letters, digits, dots, dashes or underscores." : undefined}
          value={username} onChange={(v) => { setUsername(v); start.reset(); }} />
        <NewPasswordFields label={up ? "Password" : "New password"} pw={pw} error={fieldProblem(problem, up ? "password" : "new_password")} onEdit={start.reset} />
        <Button type="submit" variant="primary" block loading={start.pending} loadingLabel="Checking…"
          disabled={!first.trim() || !username.trim() || !pw.filled || status.isPending}>
          {up ? "Continue" : "Set a new password"}
        </Button>
      </SecretForm>
      <nav className={styles.links} aria-label="Other pages">
        <Link href="/login">{up ? "I already have an account" : "Back to sign in"}</Link>
        <Link href="/privacy">How your data is protected</Link>
      </nav>
    </AuthScreen>
  );
}
