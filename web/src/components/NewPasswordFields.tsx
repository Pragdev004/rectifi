import { useState } from "react";
import { MIN_PASSWORD } from "../lib/recovery";
import { PasswordField } from "./PasswordField";

/** A new password typed twice. Kept in the page's state; `check()` says whether the two match, and shows the mismatch if not. */
export function useNewPassword() {
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [mismatch, setMismatch] = useState(false);
  return {
    password, again, mismatch,
    setPassword: (v: string) => { setPassword(v); setMismatch(false); },
    setAgain: (v: string) => { setAgain(v); setMismatch(false); },
    check: () => { const ok = password === again; setMismatch(!ok); return ok; },
    clear: () => { setPassword(""); setAgain(""); },
    filled: Boolean(password && again),
  };
}

export function NewPasswordFields({ label, pw, error, onEdit }: { label: string; pw: ReturnType<typeof useNewPassword>; error?: string | null; onEdit?: () => void }) {
  return (
    <>
      <PasswordField label={label} name="new-password" autoComplete="new-password" hint={`At least ${MIN_PASSWORD} characters.`} error={error}
        value={pw.password} onChange={(v) => { pw.setPassword(v); onEdit?.(); }} />
      <PasswordField label={`Repeat ${label.toLowerCase()}`} name="repeat-password" autoComplete="new-password"
        error={pw.mismatch ? "The two passwords are not the same." : null} value={pw.again} onChange={pw.setAgain} />
    </>
  );
}
