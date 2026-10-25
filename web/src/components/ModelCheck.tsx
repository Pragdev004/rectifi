import { Stethoscope } from "lucide-react";
import { cx } from "../lib/cx";
import { stampRows, useModelStatus, useProbeModel, type ModelProbe, type ModelRole, type StampRow } from "../api/models";
import { Button } from "./forms";
import { Callout, Disclosure, Eyebrow, Skeleton, Stamp, useDelayed } from "./ui";
import styles from "./ModelCheck.module.css";

/** The stamp rows of one check. Pure: pass it a result (or null) and it draws it. */
export function ModelStamps({ probe }: { probe: ModelProbe | null }) {
  return (
    <ul className={styles.rows} aria-label="Model check results">
      {stampRows(probe).map((row: StampRow) => (
        <li key={row.key} className={styles.row}>
          <span className={styles.label}>{row.label}</span>
          <span className={styles.stampCell}>
            <Stamp tone={row.tone}>{row.stamp}</Stamp>
            {row.detail && <span className={styles.detail}>{row.detail}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Is the model usable? Shows the quick status, and a "Test the model" button that makes real calls and says which step failed.
 *  `hideWhenOk` is for the chat page: stay out of the way until the status is not ok, or a check has been run. */
export function ModelCheck({ role = "chat", hideWhenOk = false, className }: { role?: ModelRole; hideWhenOk?: boolean; className?: string }) {
  const status = useModelStatus();
  const test = useProbeModel(role);
  const slow = useDelayed(test.isPending, 20_000);
  const probe: ModelProbe | null = test.data ?? (status.data?.probe && status.data.probe.role === role ? status.data.probe : null);

  if (hideWhenOk && status.data?.ok && !probe && !test.isPending && !test.isError) return null;

  return (
    <section className={cx(styles.root, className)} aria-labelledby="model-check-title">
      <div className={styles.head}>
        <div>
          <div className={styles.titleRow}>
            <Eyebrow as="h2"><span id="model-check-title">Model check</span></Eyebrow>
            {status.data && <Stamp tone={status.data.ok ? "ok" : "over"}>{status.data.ok ? "Ready" : "Not ready"}</Stamp>}
          </div>
          {status.data && <p className={styles.model}>{status.data.model}</p>}
        </div>
        <Button variant="secondary" icon={<Stethoscope size={16} strokeWidth={1.5} aria-hidden="true" />} loading={test.isPending}
                loadingLabel="Testing…" onClick={() => test.mutate()}>
          Test the model
        </Button>
      </div>

      {status.isPending && <Skeleton height={20} />}
      {status.data && !status.data.ok && (
        <Callout tone="over" lead="The model isn't ready" role="alert">{status.data.why}</Callout>
      )}
      {status.data?.ok && status.data.notice && <Callout tone="note">{status.data.notice}</Callout>}

      {test.isPending && (
        <p className={styles.slow} role="status">
          {slow ? "The model is still loading. The first answer on a laptop CPU can take about a minute." : "Sending a tiny message to the model…"}
        </p>
      )}
      {test.isError && <Callout tone="over" lead="The check itself failed" role="alert">{test.error.message}</Callout>}

      {probe && !test.isPending && (
        <div aria-live="polite">
          <ModelStamps probe={probe} />
          {probe.ok ? (
            <Callout tone="ok" lead="The model works">
              It answered{probe.latency_ms !== null ? ` in ${(probe.latency_ms / 1000).toFixed(1)} s` : ""}
              {probe.tools_ok ? " and called a tool." : "."}
            </Callout>
          ) : (
            <Callout tone="over" lead={failureLead(probe)} role="alert">
              {probe.hints.length > 0 && (
                <ul className={styles.hints}>{probe.hints.map((h) => <li key={h}>{h}</li>)}</ul>
              )}
            </Callout>
          )}
          {(probe.provider_message || probe.http_status) && (
            <Disclosure summary="Details from the provider">
              <p className={styles.raw}>
                {probe.http_status ? `HTTP ${probe.http_status}\n` : ""}{probe.provider_message || "(no message)"}
              </p>
            </Disclosure>
          )}
        </div>
      )}
      {!probe && !test.isPending && (
        <p className={styles.help}>A test sends one tiny message and one tool request. The first one can take about a minute while the model loads.</p>
      )}
    </section>
  );
}

const LEADS: Record<string, string> = {
  config: "The settings are not complete",
  reachable: "Rectifi can't reach the model",
  model: "The model was not found",
  capabilities: "This model can't do the job",
  chat: "The model did not reply",
  tools: "The model can't call tools",
};
function failureLead(probe: ModelProbe): string {
  return LEADS[probe.stage] ?? "The check failed";
}
