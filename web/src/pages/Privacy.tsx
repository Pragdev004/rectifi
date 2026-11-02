import { Link } from "wouter";
import { usePrivacy, type PrivacyFacts } from "../api/account";
import { useAuthStatus } from "../api/auth";
import { AuthScreen } from "../components/AuthScreen";
import { Callout, Skeleton } from "../components/ui";
import s from "./privacy.module.css";

/** The first lines of the page: what mode this is and whether the text is encrypted. Facts come from the server; this only words them. */
export function modeSentence(f: PrivacyFacts): string {
  return f.mode === "multiuser"
    ? "This Rectifi is shared. Each person signs up with an invite and has their own account."
    : "This Rectifi is in laptop mode. It has one person and nothing is encrypted. The data stays on the computer it runs on.";
}

export function encryptionSentence(f: PrivacyFacts): string {
  return f.encryption_at_rest
    ? "Text that could identify you is encrypted in the database (sealed) with a key made from your password."
    : "Nothing is encrypted in the database in laptop mode. Anyone who can open the database can read it.";
}

export function modelSentence(f: PrivacyFacts): string {
  const where = f.model_provider === "local" ? "on this server's own machine" : "in the cloud, outside this server";
  const friends = f.ai_for_friends
    ? "Friends' data can be sent to it."
    : "Friends' data is never sent to any model. The model works only for the owner.";
  return `The model that sorts payments and answers the owner's chat runs ${where} (${f.model}). ${friends}`;
}

export function PrivacyFactsView({ facts: f }: { facts: PrivacyFacts }) {
  return (
    <div className={s.page} data-testid="privacy-facts">
      <section aria-labelledby="pv-sum">
        <h2 id="pv-sum">In short</h2>
        <p className={s.lead}>{modeSentence(f)}</p>
        <p>{encryptionSentence(f)}</p>
        <p>{modelSentence(f)}</p>
        <dl className={s.facts}>
          <div><dt>Mode</dt><dd data-testid="privacy-mode">{f.mode === "multiuser" ? "Shared (multi-user)" : "Laptop (one person)"}</dd></div>
          <div><dt>Encrypted at rest</dt><dd>{f.encryption_at_rest ? "Yes" : "No"}</dd></div>
          <div><dt>Model runs</dt><dd>{f.model_provider === "local" ? "Locally" : "In the cloud"} ({f.model})</dd></div>
          <div><dt>Backups kept for</dt><dd>{f.backup_days} days</dd></div>
        </dl>
      </section>

      <section aria-labelledby="pv-who">
        <h2 id="pv-who">Who can read what</h2>
        <p className={s.muted}>
          Plain data means amounts, dates, categories, account names, last 4 digits and spending caps. Sealed text means payees, notes, SMS text and similar.
        </p>
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

      <section aria-labelledby="pv-sealed">
        <h2 id="pv-sealed">What is sealed</h2>
        <p className={s.muted}>These columns are stored encrypted for each person. The database refuses plain text in them.</p>
        <ul className={s.chips} aria-label="Sealed fields" data-testid="privacy-sealed">
          {f.sealed_fields.map((name) => <li key={name}><code>{name}</code></li>)}
        </ul>
        <h3>Keyed hashes</h3>
        <p className={s.muted}>These are one-way fingerprints, used to spot duplicates without storing the text.</p>
        <ul className={s.chips} aria-label="Keyed hash fields" data-testid="privacy-hashed">
          {f.keyed_hash_fields.map((name) => <li key={name}><code>{name}</code></li>)}
        </ul>
      </section>

      <section aria-labelledby="pv-plain">
        <h2 id="pv-plain">What stays readable, on purpose</h2>
        <p className={s.muted}>Alerts and the phone gateway must work while you are signed out, so these are plain.</p>
        <ul className={s.chips} aria-label="Plain fields" data-testid="privacy-plain">
          {f.plain_fields.map((name) => <li key={name}><code>{name}</code></li>)}
        </ul>
        <h3>Shops whose name is stored in plain text</h3>
        <p className={s.muted}>A payment to one of these well-known shops also has its name stored in plain text, so a cap on it can fire while you are signed out. Every other payee is sealed.</p>
        <ul className={s.chips} aria-label="Allow-listed shops" data-testid="privacy-brands">
          {f.merchant_allowlist.map((b) => <li key={b.key} className={s.brand}><code>{b.name}</code></li>)}
        </ul>
        <h3>The owner's own AI tables</h3>
        <p className={s.muted}>These are not sealed in this phase. They hold only the owner's own data.</p>
        <ul className={s.chips} aria-label="Owner-only tables" data-testid="privacy-owner-tables">
          {f.unsealed_owner_tables.map((name) => <li key={name}><code>{name}</code></li>)}
        </ul>
      </section>

      <section aria-labelledby="pv-limits">
        <h2 id="pv-limits">What this does not protect</h2>
        <Callout tone="near" lead="Read this before you join.">
          {f.recovery}
        </Callout>
        <ul className={s.list} data-testid="privacy-limits">
          {f.limits.map((line) => <li key={line}>{line}</li>)}
        </ul>
      </section>
    </div>
  );
}

export default function PrivacyPage() {
  const facts = usePrivacy();
  const status = useAuthStatus();
  const inside = status.data ? !status.data.required || status.data.signed_in : false;
  const back = inside ? { to: "/", label: "Back to Rectifi" } : { to: "/login", label: "Back to sign in" };
  return (
    <AuthScreen eyebrow="Privacy" title="How your data is protected" wide>
      {facts.data ? (
        <PrivacyFactsView facts={facts.data} />
      ) : facts.isError ? (
        <p className={s.errorText} role="alert">Can't reach Rectifi right now. Reload this page to try again.</p>
      ) : (
        <Skeleton height={200} />
      )}
      <Link href={back.to} className={s.back}>{back.label}</Link>
    </AuthScreen>
  );
}
