import { Download } from "lucide-react";
import { useState, type ReactNode } from "react";
import { canFinish, recoveryFileName, recoveryFileText, saveText } from "../lib/recovery";
import { Button, Checkbox } from "./forms";
import { Callout, CodeValue } from "./ui";
import styles from "./RecoveryGate.module.css";

/** Shows a recovery code once and holds the Finish button until the person has either downloaded the code or ticked "I saved it".
 *  The code is a prop. It is never written to storage, the URL or the query cache; the parent drops it when Finish succeeds. */
export function RecoveryGate({
  code, username, finishLabel, finishing, finishingLabel, onFinish, error, intro,
}: {
  code: string;
  username: string;
  finishLabel: string;
  finishing: boolean;
  finishingLabel: string;
  onFinish: () => void;
  error?: string | null;
  intro?: ReactNode;
}) {
  const [downloaded, setDownloaded] = useState(false);
  const [saved, setSaved] = useState(false);
  const open = canFinish({ downloaded, saved });

  function download() {
    saveText(recoveryFileText(username, code), recoveryFileName(username));
    setDownloaded(true);
  }

  return (
    <div className={styles.gate} data-testid="recovery-gate">
      <Callout tone="near" lead="This is the only time you will see this code.">
        <div className={styles.warn}>
          {intro}
          <p>If you forget your password, this code is the only way back in. There is no email and no reset, and nobody can reset it for you.</p>
        </div>
      </Callout>
      <div className={styles.code}>
        <CodeValue value={code} label="recovery code" wrap />
      </div>
      <div className={styles.actions}>
        <Button variant="secondary" block icon={<Download size={16} strokeWidth={1.5} aria-hidden="true" />} onClick={download}>
          Download the code (.txt)
        </Button>
        <Checkbox label="I saved it somewhere safe" checked={saved} onChange={setSaved} />
        <p className={styles.state} role="status">
          {downloaded ? "Downloaded." : open ? "Marked as saved." : `Download the code or tick the box to go on.`}
        </p>
      </div>
      {error && <Callout tone="over" role="alert">{error}</Callout>}
      <Button variant="primary" block disabled={!open} loading={finishing} loadingLabel={finishingLabel} onClick={onFinish}>
        {finishLabel}
      </Button>
    </div>
  );
}
