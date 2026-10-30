import { Globe } from "lucide-react";
import { Button } from "../forms";
import { Callout } from "../ui";
import type { WebConsentRequest } from "../../api/knowledge";

/** The card shown when the answer graph pauses to ask before a web search. Only the rewritten, generic question is sent. */
export function WebConsent({ request, busy, onChoose }: { request: WebConsentRequest; busy?: boolean; onChoose: (allow: boolean) => void }) {
  return (
    <Callout
      tone="note"
      icon={Globe}
      role="status"
      lead={`Search the web for “${request.query}”?`}
      actions={
        <>
          <Button variant="primary" loading={busy} loadingLabel="Searching…" onClick={() => onChoose(true)}>Search</Button>
          <Button variant="secondary" disabled={busy} onClick={() => onChoose(false)}>Answer without it</Button>
        </>
      }
    >
      Only this question is sent to DuckDuckGo. Nothing about your money is sent.
    </Callout>
  );
}
