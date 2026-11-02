import { Eye, EyeOff } from "lucide-react";
import { useState, type Ref } from "react";
import { Field, TextInput } from "./forms";
import styles from "./AuthScreen.module.css";

/** A password box with a show/hide button. The value belongs to the caller's state and is cleared by the caller after it is sent. */
export function PasswordField({
  label, value, onChange, autoComplete, error, hint, name, inputRef, autoFocus,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: "current-password" | "new-password";
  error?: string | null;
  hint?: string;
  name: string;
  inputRef?: Ref<HTMLInputElement>;
  autoFocus?: boolean;
}) {
  const [shown, setShown] = useState(false);
  return (
    <Field label={label} error={error} hint={hint}>
      {(field) => (
        <div className={styles.pw}>
          <TextInput
            {...field}
            ref={inputRef}
            className={styles.pwInput}
            type={shown ? "text" : "password"}
            name={name}
            autoComplete={autoComplete}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            autoFocus={autoFocus}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
          <button type="button" className={styles.reveal} aria-label={shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`} aria-pressed={shown} onClick={() => setShown((s) => !s)}>
            {shown ? <EyeOff size={18} strokeWidth={1.5} aria-hidden="true" /> : <Eye size={18} strokeWidth={1.5} aria-hidden="true" />}
          </button>
        </div>
      )}
    </Field>
  );
}
