import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type StageShellProps = {
  /** Stage heading. */
  title: string;
  /** Optional sub-heading rendered below the title. */
  subtitle?: ReactNode;
  /** Stage body (forms, lists, previews, etc.). */
  body: ReactNode;
  /** Button label. Defaults to `Continue`. */
  continueLabel?: string;
  /** Whether the gate for this stage is satisfied. Disables the CTA when false. */
  canContinue: boolean;
  /** Invoked when the operator hits the CTA. */
  onContinue?: () => void;
  /** Hide the footer Continue button entirely. For stages that drive their own
   *  primary action from the body (e.g. Review's approve/reject gate), where a
   *  generic permanently-disabled "Continue" would be dead, confusing UI. */
  hideContinue?: boolean;
  /** Optional secondary slot rendered next to the primary CTA (e.g. cancel). */
  secondaryAction?: ReactNode;
  /** Override the outer wrapper classes. */
  className?: string;
};

/**
 * Standard chrome for a pipeline stage. Wraps the body in a shadcn-style
 * card with a sticky header and a bottom-right CTA. Disable the CTA via
 * `canContinue={false}` until the stage gate is satisfied.
 *
 * Real per-stage components in PF-B/C/D/E/F slot their inputs into `body`
 * and own the `canContinue` derivation.
 */
export function StageShell({
  title,
  subtitle,
  body,
  continueLabel = "Continue",
  canContinue,
  onContinue,
  hideContinue = false,
  secondaryAction,
  className,
}: StageShellProps) {
  return (
    <section
      className={cn(
        "flex flex-col gap-6 rounded-lg border border-border bg-card text-card-foreground shadow-sm",
        className,
      )}
    >
      <header className="flex flex-col gap-1 border-b border-border px-5 py-4 sm:px-6">
        <h2 className="text-lg font-semibold tracking-tight sm:text-xl">{title}</h2>
        {subtitle ? <p className="text-sm text-muted-foreground">{subtitle}</p> : null}
      </header>
      <div className="px-5 pb-6 sm:px-6">{body}</div>
      {/* Render the footer unless there's nothing to put in it. `hideContinue`
          drops the primary CTA for stages that drive their own action from the
          body (e.g. Review's approve/reject gate); a secondaryAction can still
          keep the footer. */}
      {!hideContinue || secondaryAction ? (
        <footer className="flex flex-col-reverse items-stretch justify-end gap-2 border-t border-border px-5 py-3 sm:flex-row sm:items-center sm:px-6">
          {secondaryAction}
          {!hideContinue ? (
            <Button
              type="button"
              disabled={!canContinue}
              onClick={onContinue}
              aria-disabled={!canContinue}
            >
              {continueLabel}
            </Button>
          ) : null}
        </footer>
      ) : null}
    </section>
  );
}
