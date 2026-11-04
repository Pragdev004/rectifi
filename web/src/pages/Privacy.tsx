import { Link } from "wouter";
import { usePrivacy, type PrivacyFacts } from "../api/account";
import { AuthScreen } from "../components/AuthScreen";
import { Callout } from "../components/ui";
import s from "./privacy.module.css";

/** The first lines of the page: what mode this is and whether the text is encrypted. Facts come from the server; this only words them. */
export function modeSentence(f: PrivacyFacts): string {
  return f.mode === "multiuser"
    ? "This Rectifi is shared. Each person signs up with an invite."
    : "This Rectifi is in laptop mode: one person, nothing is encrypted, and the data stays on this computer.";
}

export function encryptionSentence(f: PrivacyFacts): string {
  return f.encryption_at_rest
    ? "Text that could identify you is sealed (encrypted) with a key made from your password."
    : "Anyone who can open the database can read it.";
}

export function modelSentence(f: PrivacyFacts): string {
  const where = f.model_provider === "local" ? "on this server's own machine" : "in the cloud, outside this server";
  const friends = f.ai_for_friends ? "Friends' data can be sent to it." : "Friends' data is never sent to any model.";
  return `The model that sorts payments and answers the owner's chat runs ${where} (${f.model}). ${friends}`;
}

function Group({ title, note, names, brand }: { title: string; note: string; names: string[]; brand?: boolean }) {
  return (
    <>
      <h3>{title}</h3>
      <p className={s.muted}>{note}</p>
      <ul className={s.chips} aria-label={title}>
        {names.map((name) => <li key={name} className={brand ? s.brand : undefined}><code>{name}</code></li>)}
      </ul>
    </>
  );
}

export function PrivacyFactsView({ facts: f }: { facts: PrivacyFacts }) {
  return (
    <div className={s.page} data-testid="privacy-facts">
      <section>
        <h2>In short</h2>
        <p className={s.lead}>{modeSentence(f)}</p>
        <p>{encryptionSentence(f)}</p>
        <p>{modelSentence(f)}</p>
        <p>Backups are kept for {f.backup_days} days.</p>
      </section>
      <section>
        <h2>Who can read what</h2>
        <ul className={s.rows}>
          {f.who_can_read.map((row) => (
            <li key={row.who} className={s.row}>
              <h3>{row.who}</h3>
              <p><span className={s.label}>Plain data: </span>{row.plain}</p>
              <p><span className={s.label}>Sealed text: </span>{row.sealed}</p>
            </li>
          ))}
        </ul>
      </section>
      <section>
        <h2>What is stored how</h2>
        <Group title="Sealed" names={f.sealed_fields} note="Encrypted per person." />
        <Group title="Keyed hashes" names={f.keyed_hash_fields}
          note="One-way fingerprints. Those from your own key can't be guessed from a database copy. UPI references and SMS also need the server's secret blind key, which is not in the database or its backups: a copy of the database alone can't test guesses, but the server owner (who has both) could." />
        <Group title="Shop names hidden inside longer keys" names={f.hidden_key_fields}
          note="A shop that is not on the list below is replaced by a hash made from your own key." />
        <Group title="Plain, on purpose" names={f.plain_fields} note="Alerts must work while you are signed out." />
        <Group title="Shops stored by name" names={f.merchant_allowlist.map((b) => b.name)} brand
          note="These shop names are plain, so a cap on them can fire while you are signed out." />
        <Group title="The owner's own AI tables" names={f.unsealed_owner_tables} note="Not sealed yet. Only the owner's data." />
      </section>
      <section>
        <h2>What this does not protect</h2>
        <Callout tone="near">{f.recovery}</Callout>
        <ul className={s.list}>
          {f.limits.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </section>
    </div>
  );
}

export default function PrivacyPage() {
  const facts = usePrivacy();
  return (
    <AuthScreen eyebrow="Privacy" title="How your data is protected" wide>
      {facts.data ? (
        <PrivacyFactsView facts={facts.data} />
      ) : (
        <p className={s.muted} role="status">{facts.isError ? "Can't reach Rectifi right now. Reload to try again." : "Loading…"}</p>
      )}
      <Link href="/" className={s.back}>Back to Rectifi</Link>
    </AuthScreen>
  );
}
