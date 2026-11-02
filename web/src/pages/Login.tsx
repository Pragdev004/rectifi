import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useSearch } from "wouter";
import { safeNext, signInProblem, useAuthStatus, useSignIn } from "../api/auth";
import { AuthScreen, authStyles as styles } from "../components/AuthScreen";
import { Button, Field, TextInput } from "../components/forms";
import { PasswordField } from "../components/PasswordField";
import { Callout } from "../components/ui";

export default function LoginPage() {
  const [, navigate] = useLocation();
  const next = safeNext(new URLSearchParams(useSearch()).get("next"));
  const status = useAuthStatus();
  const signIn = useSignIn();
  const multi = status.data?.multiuser === true;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const input = useRef<HTMLInputElement>(null);

  // Nothing to sign in to (a laptop with no password), or already signed in: go on to the app.
  const done = status.data ? !status.data.required || status.data.signed_in : false;
  useEffect(() => {
    if (done) navigate(next, { replace: true });
  }, [done, navigate, next]);

  const problem = signIn.error ? signInProblem(signIn.error) : null;
  const ready = Boolean(password) && (!multi || Boolean(username.trim()));

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!ready || signIn.pending) return;
    const out = await signIn.run(multi ? { username: username.trim(), password } : { password });
    if (out) {
      setPassword(""); // the password is not kept once it has been sent
      navigate(next, { replace: true });
    } else {
      input.current?.focus();
    }
  }

  return (
    <AuthScreen title="Sign in">
      {done ? (
        <p className={styles.status} role="status">Taking you to Rectifi…</p>
      ) : (
        <form className={styles.form} onSubmit={(e) => void submit(e)} noValidate>
          {problem?.locked && <Callout tone="near" role="alert">{problem.message}</Callout>}
          {multi && (
            <Field label="Username">
              {(field) => (
                <TextInput
                  {...field}
                  name="username"
                  autoComplete="username"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  autoFocus
                  value={username}
                  onChange={(e) => {
                    setUsername(e.target.value);
                    if (signIn.error) signIn.reset();
                  }}
                />
              )}
            </Field>
          )}
          <PasswordField
            label="Password"
            name="password"
            inputRef={input}
            autoComplete="current-password"
            autoFocus={!multi}
            disabled={status.isPending}
            error={problem && !problem.locked ? problem.message : null}
            value={password}
            onChange={(v) => {
              setPassword(v);
              if (signIn.error) signIn.reset();
            }}
          />
          <Button type="submit" variant="primary" block loading={signIn.pending} loadingLabel="Signing in…" disabled={!ready || status.isPending}>
            Sign in
          </Button>
          {status.isError && (
            <Callout tone="near" role="alert">Can't reach Rectifi right now. Check your connection, then reload this page.</Callout>
          )}
        </form>
      )}
      {multi ? (
        <nav className={styles.links} aria-label="Other ways in">
          <Link href="/signup">I have an invite code</Link>
          <Link href="/recover">Forgot my password</Link>
          <Link href="/privacy">How your data is protected</Link>
        </nav>
      ) : (
        <p className={styles.help}>
          Forgot it? On the server, run <code>python scripts/dev.py set-password</code>.
        </p>
      )}
    </AuthScreen>
  );
}
