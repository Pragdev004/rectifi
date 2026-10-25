import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { get } from "../api/client";
import type { InboxPrefill } from "../api/types";
import { ImportStatement } from "../components/add/ImportStatement";
import { PasteSms } from "../components/add/PasteSms";
import { QuickEntry } from "../components/add/QuickEntry";
import { Button, panelId, Tabs, tabId } from "../components/forms";
import { PageHeader } from "../components/PageHeader";
import { Callout } from "../components/ui";
import { useUrlState } from "../lib/urlState";
import addStyles from "../components/add/add.module.css";

type TabId = "quick" | "sms" | "import";
const TABS: { id: TabId; label: string }[] = [
  { id: "quick", label: "Quick entry" },
  { id: "sms", label: "Paste bank SMS" },
  { id: "import", label: "Import statement" },
];

export default function AddPage() {
  const [url, setUrl] = useUrlState({ tab: "quick", inbox: "" });
  const tab: TabId = (TABS.find((t) => t.id === url.tab)?.id ?? "quick") as TabId;
  const inboxId = url.inbox && /^\d+$/.test(url.inbox) ? url.inbox : "";
  const prefill = useQuery({
    queryKey: ["inbox-prefill", inboxId],
    queryFn: ({ signal }) => get<InboxPrefill>(`/inbox/${inboxId}/prefill`, signal),
    enabled: inboxId !== "",
    retry: false,
    staleTime: Infinity,
  });
  const pre = prefill.data ?? null;

  return (
    <>
      <PageHeader name="Add" title="Add" />
      {inboxId && prefill.isError && (
        <Callout tone="near" role="status">That bank SMS is no longer in the inbox. {prefill.error instanceof Error ? prefill.error.message : ""}</Callout>
      )}
      {pre && (
        <div className={addStyles.panel} style={{ marginTop: 0, marginBottom: "var(--s-5)" }}>
          <Callout
            tone="note"
            lead="Filled in from a bank SMS your phone sent."
            actions={<Button variant="secondary" icon={<X size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={() => setUrl({ inbox: "" })}>Forget this SMS</Button>}
          >
            Rectifi{"’"}s fixed rules could not read all of it, so check every field, then save.
          </Callout>
          <pre className={addStyles.rawSms}>{pre.raw_text}</pre>
        </div>
      )}
      <Tabs group="add" label="Ways to add" tabs={TABS} value={tab} onChange={(id) => setUrl({ tab: id }, { replace: true })} />
      <div role="tabpanel" id={panelId("add", tab)} aria-labelledby={tabId("add", tab)}>
        {tab === "quick" && <QuickEntry prefill={pre} onSavedFromInbox={() => setUrl({ inbox: "" }, { replace: true })} />}
        {tab === "sms" && <PasteSms />}
        {tab === "import" && <ImportStatement />}
      </div>
    </>
  );
}
