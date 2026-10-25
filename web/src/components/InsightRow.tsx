import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Lightbulb } from "lucide-react";
import { ApiError, post } from "../api/client";
import { keys, refreshBooks } from "../api/queries";
import type { ExplainResult, Insight } from "../api/types";
import { cx } from "../lib/cx";
import { hrefFor, pageHref } from "../lib/links";
import { formatInr } from "../lib/money";
import { Button, LinkButton } from "./forms";
import { useToast } from "./Toast";
import { Eyebrow, Stamp, type Tone } from "./ui";
import { useState } from "react";
import styles from "./InsightRow.module.css";

const SEVERITY: Record<Insight["severity"], { tone: Tone; word: string }> = {
  act: { tone: "over", word: "Act" },
  watch: { tone: "near", word: "Watch" },
  info: { tone: "accent", word: "Note" },
};

export function InsightRow({ insight, compact, explainOk }: { insight: Insight; compact?: boolean; explainOk?: boolean }) {
  const client = useQueryClient();
  const toast = useToast();
  const [explained, setExplained] = useState<ExplainResult | null>(null);
  const sev = SEVERITY[insight.severity];

  const apply = useMutation({
    mutationFn: () => post<{ category: string; month: string; limit: string }>("/insights/apply", { key: insight.key }),
    onSuccess: (r) => {
      toast(`Budget set for ${r.category}.`);
      refreshBooks(client);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.status === 404) {
        toast("This suggestion changed. Refreshed.", "note");
        void client.invalidateQueries({ queryKey: keys.insights });
        refreshBooks(client);
      } else {
        toast(e instanceof Error ? e.message : "Could not set that budget.", "over");
      }
    },
  });

  const explain = useMutation({
    mutationFn: () => post<ExplainResult>("/insights/explain", { key: insight.key }),
    onSuccess: setExplained,
    onError: (e) => setExplained({ text: null, dropped: [], error: e instanceof Error ? e.message : "The assistant could not answer." }),
  });

  const href = hrefFor(insight.link) ?? pageHref(insight.action_page);
  const link = !insight.can_apply && href && insight.action_label ? href : null;

  return (
    <div className={cx(styles.row, compact && styles.compact)} data-testid="insight-row" data-key={insight.key}>
      <div className={styles.stampCol}><Stamp tone={sev.tone}>{sev.word}</Stamp></div>
      <div>
        <p className={styles.title}>{insight.title}</p>
        {!compact && <p className={styles.detail}>{insight.detail}</p>}
        <div className={styles.actions}>
          {insight.can_apply && (
            <Button variant="primary" loading={apply.isPending} loadingLabel="Setting…" onClick={() => apply.mutate()}>
              {insight.action_label ?? "Set this budget"}
            </Button>
          )}
          {link && <LinkButton to={link} variant={compact ? "tertiary" : "secondary"} icon={<ArrowRight size={16} strokeWidth={1.5} aria-hidden="true" />}>{insight.action_label}</LinkButton>}
          {!compact && explainOk && (
            <Button variant="tertiary" loading={explain.isPending} loadingLabel="Thinking…" icon={<Lightbulb size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => explain.mutate()}>
              Explain this
            </Button>
          )}
        </div>
        {explained && <ExplainNote result={explained} />}
      </div>
    </div>
  );
}

function ExplainNote({ result }: { result: ExplainResult }) {
  if (result.text) {
    return (
      <div className={styles.note} role="status">
        <Eyebrow as="span" className={styles.label}>Assistant's note {"·"} every figure checked against the numbers above</Eyebrow>
        <p>{result.text}</p>
      </div>
    );
  }
  if (result.dropped.length) {
    const shown = result.dropped.map((v) => formatInr(v, { paise: false })).join(", ");
    return (
      <div className={cx(styles.note, styles.noteNear)} role="status">
        <p><strong>Not shown:</strong> the assistant used {shown}, which Rectifi did not calculate. Its advice was thrown away so no unchecked figure reaches you.</p>
      </div>
    );
  }
  return (
    <div className={cx(styles.note, styles.noteMuted)} role="status">
      <p>{result.error ?? "The assistant gave no answer."}</p>
    </div>
  );
}
