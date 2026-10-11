import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../theme";

// The overview query fails: only the error state renders, no canvas.
vi.mock("../lib/project", () => ({
  useProjectQuery: () => ({ data: undefined, isLoading: false, isError: true, error: new Error("boom"), refetch: vi.fn() }),
}));

const { Overview } = await import("../components/Overview");

describe("the Explorer graph error", () => {
  it("is titled as the graph's, never 'the overview' (which reads like the project Overview page)", () => {
    render(
      <ThemeProvider>
        <Overview />
      </ThemeProvider>,
    );
    expect(screen.getByText("Couldn't load the graph")).toBeInTheDocument();
    expect(screen.queryByText(/load the overview/)).toBeNull();
  });
});
