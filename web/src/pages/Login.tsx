import { Eye, EyeOff } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useLocation, useSearch } from "wouter";
import { safeNext, signInProblem, useAuthStatus, useSignIn } from "../api/auth";
import { Button, Field, TextInput } from "../components/forms";
import { Callout, Eyebrow } from "../components/ui";
import styles from "./login.module.css";

export default function LoginPage() {
  const [, navigate] = useLocation();
  const next = safeNext(new URLSearchParams(useSearch()).get("next"));
  const status = useAuthStatus();
  const signIn = useSignIn();
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const noteId = useId();

  // Nothing to sign in to (a laptop with no password), or already signed in: go on to the app.
  const done = status.data ? !status.data.required || status.data.signed_in : false;
  useEffect(() => {
    if (done) navigate(next, { replace: true });
  }, [done, navigate, next]);

  const problem = signIn.isError ? signInProblem(signIn.error) : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!password || signIn.isPending) return;
    signIn.mutate(password, {
      onSuccess: () => {
        setPassword(""); // the password is not kept once it has been sent
        navigate(next, { replace: true });
      },
      onError: () => input.current?.focus(),
    });
  }

  return (
    <main className={styles.screen}>
      <div className={styles.inner}>
        <div className={styles.brand}>
          <p className={styles.wordmark}>Rectifi</p>
          <p className={styles.tagline}>every rupee, accounted for</p>
        </div>
        <section className={styles.slip} aria-labelledby={`${noteId}-h`}>
          <div>
            <Eyebrow>Private</Eyebrow>
            <h1 id={`${noteId}-h`}>Sign in</h1>
          </div>
          {done ? (
            <p className={styles.status} role="status">Taking you to Rectifi…</p>
          ) : (
            <form className={styles.form} onSubmit={submit} noValidate>
              {problem?.locked && <Callout tone="near" role="alert">{problem.message}</Callout>}
              <Field label="Password" error={problem && !problem.locked ? problem.message : null}>
                {(field) => (
                  <div className={styles.pw}>
                    <TextInput
                      {...field}
                      ref={input}
                      className={styles.pwInput}
                      type={shown ? "text" : "password"}
                      name="password"
                      autoComplete="current-password"
                      autoCapitalize="off"
                      autoCorrect="off"
                      spellCheck={false}
                      autoFocus
                      value={password}
                      disabled={status.isPending}
                      onChange={(e) => {
                        setPassword(e.target.value);
                        if (signIn.isError) signIn.reset();
                      }}
                    />
                    <button
                      type="button"
                      className={styles.reveal}
                      aria-label={shown ? "Hide password" : "Show password"}
                      aria-pressed={shown}
                      onClick={() => setShown((s) => !s)}
                    >
                      {shown ? <EyeOff size={18} strokeWidth={1.5} aria-hidden="true" /> : <Eye size={18} strokeWidth={1.5} aria-hidden="true" />}
                    </button>
                  </div>
                )}
              </Field>
              <Button type="submit" variant="primary" block loading={signIn.isPending} loadingLabel="Signing in…" disabled={!password || status.isPending}>
                Sign in
              </Button>
              {status.isError && (
                <Callout tone="near" role="alert">Can't reach Rectifi right now. Check your connection, then reload this page.</Callout>
              )}
            </form>
          )}
          <p className={styles.help}>
            Forgot it? On the server, run <code>python scripts/dev.py set-password</code>.
          </p>
        </section>
      </div>
    </main>
  );
}
