/** The recovery code screen's rules. Plain functions, so the gate can be tested without a browser. */

/** Finish stays disabled until the person has downloaded the code or ticked "I saved it". */
export function canFinish(state: { downloaded: boolean; saved: boolean }): boolean {
  return state.downloaded || state.saved;
}

export const RECOVERY_FILE = "rectifi-recovery-code.txt";

/** The text of the downloaded file. It says what the code is for, because a file called "code.txt" found a year later must explain itself. */
export function recoveryFileText(username: string, code: string): string {
  return [
    "Rectifi recovery code",
    "",
    `Account: ${username}`,
    `Code:    ${code}`,
    "",
    "The only way back in if you forget your password. There is no email and no reset.",
    "Each code works once. Keep this file private.",
    "",
  ].join("\r\n");
}

/** Hand a Blob to the browser as a download. The object URL is revoked straight after. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function saveText(text: string, filename: string): void {
  saveBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), filename);
}

/** The shortest password the server accepts (it checks too; this is only for the hint). */
export const MIN_PASSWORD = 12;
