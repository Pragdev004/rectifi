import type { ReactNode } from "react";
import { formProblem, type Problem } from "../api/secret";
import { Callout } from "./ui";

/** A form that holds a secret. A problem that no single field owns shows above the fields. */
export function SecretForm({ problem, fields, onSubmit, className, children }: {
  problem: Problem | null; fields: string[]; onSubmit: () => void | Promise<void>; className?: string; children: ReactNode;
}) {
  const note = formProblem(problem, fields);
  return (
    <form className={className} noValidate onSubmit={(e) => { e.preventDefault(); void onSubmit(); }}>
      {note && <Callout tone="near" role="alert">{note}</Callout>}
      {children}
    </form>
  );
}
