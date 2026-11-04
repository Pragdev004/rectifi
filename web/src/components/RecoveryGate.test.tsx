import { describe, expect, it } from "vitest";
import { renderWith } from "../test/render";
import { RecoveryGate } from "./RecoveryGate";

function html() {
  return renderWith(
    <RecoveryGate code="ABCD-EFGH-JKLM" username="asha" finishLabel="Finish sign-up" finishing={false} finishingLabel="Finishing…" onFinish={() => {}} />,
    null,
  );
}

describe("RecoveryGate", () => {
  it("shows the code once, with a download, an unticked box and a Finish button that is off", () => {
    const out = html();
    expect(out).toContain("ABCD-EFGH-JKLM");
    expect(out).toContain("Download the code (.txt)");
    expect(out).toContain("I saved it somewhere safe");
    expect(out).not.toMatch(/type="checkbox"[^>]*checked/);
    const finish = /<button[^>]*>Finish sign-up<\/button>/.exec(out)?.[0] ?? "";
    expect(finish).toContain('aria-disabled="true"');
  });
  it("says in words that this is the only time and there is no reset", () => {
    const out = html();
    expect(out).toMatch(/only time you will see this code/i);
    expect(out).toMatch(/no email and no reset/i);
  });
});
