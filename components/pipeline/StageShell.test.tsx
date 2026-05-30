/**
 * StageShell is the chrome (header + body + footer with CTA) for every
 * pipeline stage. Pure presentational; quick tests.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { StageShell } from "./StageShell";

describe("StageShell", () => {
  it("renders title + subtitle + body", () => {
    render(
      <StageShell title="Stage X" subtitle="Do the thing" canContinue body={<p>Body content</p>} />,
    );
    expect(screen.getByRole("heading", { name: "Stage X" })).toBeInTheDocument();
    expect(screen.getByText("Do the thing")).toBeInTheDocument();
    expect(screen.getByText("Body content")).toBeInTheDocument();
  });

  it("renders the default Continue label", () => {
    render(<StageShell title="X" canContinue onContinue={() => {}} body={null} />);
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
  });

  it("uses the custom continueLabel when provided", () => {
    render(
      <StageShell title="X" canContinue continueLabel="Next" onContinue={() => {}} body={null} />,
    );
    expect(screen.getByRole("button", { name: "Next" })).toBeInTheDocument();
  });

  it("disables the CTA when canContinue is false", () => {
    render(<StageShell title="X" canContinue={false} onContinue={() => {}} body={null} />);
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("shows a disabled Continue by default even without onContinue", () => {
    // StagePlaceholder relies on this: a stage with no handler still shows a
    // disabled Continue as the "gate not satisfied yet" affordance.
    render(<StageShell title="X" canContinue={false} body={null} />);
    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("renders NO Continue button when hideContinue is set", () => {
    // Stages that drive their own action from the body (e.g. Review's
    // approve/reject gate) pass hideContinue; the shell must not show a dead,
    // permanently-disabled Continue button below them.
    render(<StageShell title="X" canContinue={false} hideContinue body={<p>body</p>} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("calls onContinue when the CTA is clicked", async () => {
    const onContinue = vi.fn();
    const user = userEvent.setup();
    render(<StageShell title="X" canContinue onContinue={onContinue} body={null} />);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onContinue).toHaveBeenCalled();
  });

  it("renders the secondaryAction slot", () => {
    render(
      <StageShell
        title="X"
        canContinue
        body={null}
        secondaryAction={<span data-testid="secondary">cancel</span>}
      />,
    );
    expect(screen.getByTestId("secondary")).toBeInTheDocument();
  });

  it("does not render subtitle when omitted", () => {
    render(<StageShell title="X" canContinue body={null} />);
    expect(screen.queryByText(/subtitle/i)).not.toBeInTheDocument();
  });
});
