import { Field, TextInput } from "./forms";

/** A labelled text box for names and codes: no auto-capitals, no spell check, so a code is never "fixed" by the keyboard. */
export function TextBox({
  label, value, onChange, name, autoComplete = "off", hint, error, autoFocus,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  name: string;
  autoComplete?: "off" | "username";
  hint?: string;
  error?: string | null;
  autoFocus?: boolean;
}) {
  return (
    <Field label={label} hint={hint} error={error}>
      {(field) => (
        <TextInput {...field} name={name} autoComplete={autoComplete} autoCapitalize="off" autoCorrect="off" spellCheck={false}
          autoFocus={autoFocus} value={value} onChange={(e) => onChange(e.target.value)} />
      )}
    </Field>
  );
}
