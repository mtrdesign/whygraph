import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { StatusPill } from "../components/ui/status-pill";

describe("StatusPill", () => {
  it.each([
    ["ok", "bg-success-soft"],
    ["warn", "bg-warning-soft"],
    ["error", "bg-destructive-soft"],
    ["info", "bg-info-soft"],
    ["busy", "bg-primary-soft"],
  ] as const)("renders the %s tone as a soft badge", (tone, soft) => {
    render(<StatusPill tone={tone} label="Label" />);
    expect(screen.getByText("Label")).toHaveClass(soft);
  });

  it("pulses only when asked", () => {
    const { container, rerender } = render(<StatusPill tone="busy" label="Running" />);
    expect(container.querySelector(".animate-pulse")).toBeNull();
    rerender(<StatusPill tone="busy" label="Running" pulse />);
    expect(container.querySelector(".animate-pulse")).not.toBeNull();
  });
});

describe("soft variants", () => {
  it("Badge brand and info", () => {
    render(
      <>
        <Badge variant="brand">b</Badge>
        <Badge variant="info">i</Badge>
      </>,
    );
    expect(screen.getByText("b")).toHaveClass("bg-primary-soft", "text-primary-text");
    expect(screen.getByText("i")).toHaveClass("bg-info-soft", "text-info");
  });

  it.each(["warning", "info", "success", "destructive"] as const)("Alert %s", (variant) => {
    render(<Alert variant={variant}>x</Alert>);
    expect(screen.getByRole("alert")).toHaveClass(`bg-${variant}-soft`);
  });
});
