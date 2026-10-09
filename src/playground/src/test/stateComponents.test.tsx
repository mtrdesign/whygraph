import { act, render, screen } from "@testing-library/react";
import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, setErrorMode } from "../api";
import { NotFoundPage } from "../components/portal/EdgeStates";
import { DisabledReason } from "../components/state/DisabledReason";
import { ErrorState } from "../components/state/ErrorState";
import { PortalErrorPage } from "../components/state/PortalErrorPage";
import { QueryState } from "../components/state/QueryState";
import { TooltipProvider } from "../components/ui/tooltip";
import { hardNavigate } from "../lib/navigation";

afterEach(() => {
  setErrorMode("local");
  vi.unstubAllGlobals();
});

// Components with a <Link> need a router context; a bare root route is enough.
async function renderWithRouter(ui: React.ReactNode) {
  const root = createRootRoute({ component: () => <>{ui}</> });
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ["/"] }) });
  await act(async () => {
    render(<RouterProvider router={router} />);
  });
}

type Q = { data: string[] | undefined; error: unknown; isError: boolean; refetch: () => Promise<unknown> };
const query = (over: Partial<Q> = {}): Q => ({
  data: undefined,
  error: null,
  isError: false,
  refetch: vi.fn(() => Promise.resolve()),
  ...over,
});

function Gate({ q }: { q: Q }) {
  return (
    <QueryState
      query={q as never}
      loading={<p>skeleton</p>}
      isEmpty={(d: string[]) => d.length === 0}
      empty={<p>nothing yet</p>}
      errorTitle="Couldn't load things"
      forbidden={{ what: "the audit log", grant: "owner" }}
      notFound="run"
    >
      {(d: string[]) => <p>{d.join(",")}</p>}
    </QueryState>
  );
}

// ---- QueryState ---------------------------------------------------------------------------

describe("QueryState", () => {
  it("shows the skeleton while there is no data", async () => {
    await renderWithRouter(<Gate q={query()} />);
    expect(screen.getByText("skeleton")).toBeInTheDocument();
  });

  it("renders children with the data, and the empty state for empty data", async () => {
    await renderWithRouter(<Gate q={query({ data: ["a", "b"] })} />);
    expect(screen.getByText("a,b")).toBeInTheDocument();
  });

  it("renders the empty state for empty data", async () => {
    await renderWithRouter(<Gate q={query({ data: [] })} />);
    expect(screen.getByText("nothing yet")).toBeInTheDocument();
  });

  it("shows a 403 as ForbiddenState in production, naming who can grant it", async () => {
    setErrorMode("production");
    await renderWithRouter(<Gate q={query({ isError: true, error: new ApiError(403, "x", "forbidden") })} />);
    expect(screen.getByTestId("empty-state")).toHaveTextContent(
      "You don't have access to the audit log. An organization owner can give you access.",
    );
  });

  it("shows a local 403 as an error, never as a permission state", async () => {
    await renderWithRouter(<Gate q={query({ isError: true, error: new ApiError(403, "x", "forbidden") })} />);
    expect(screen.getByTestId("error-state")).toHaveTextContent("This action isn't available here.");
  });

  it("shows a 404 as the kind of NotFoundState it was given", async () => {
    await renderWithRouter(<Gate q={query({ isError: true, error: new ApiError(404, "x", "not_found") })} />);
    expect(screen.getByTestId("not-found")).toHaveAttribute("data-kind", "run");
    expect(screen.getByRole("heading", { name: "Scan run not found" })).toBeInTheDocument();
  });

  it("shows any other error with Retry, which refetches", async () => {
    const q = query({ isError: true, error: new ApiError(500, "boom") });
    await renderWithRouter(<Gate q={q} />);
    const state = screen.getByTestId("error-state");
    expect(state).toHaveTextContent("Couldn't load things");
    expect(state).toHaveTextContent("WhyGraph hit an error");
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(q.refetch).toHaveBeenCalledTimes(1);
  });
});

// ---- ErrorState ----------------------------------------------------------------------------

describe("ErrorState", () => {
  it("keeps the server's text behind Show details", async () => {
    await renderWithRouter(<ErrorState error={new ApiError(409, "the path is not a git repository", "not_git")} />);
    expect(screen.getByTestId("error-state")).toHaveTextContent("This folder is not a git repository.");
    const details = screen.getByTestId("error-details");
    expect(details).not.toHaveAttribute("open");
    expect(details).toHaveTextContent("not_git");
    expect(details).toHaveTextContent("the path is not a git repository");
  });

  it("offers the registry's next step as a link", async () => {
    await renderWithRouter(<ErrorState error={new ApiError(0, "x", "no_llm_key", { provider: "openai" })} />);
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/settings");
  });
});

// ---- PortalErrorPage -------------------------------------------------------------------------

describe("PortalErrorPage", () => {
  it("is branded, gives the local hint and offers Try again and Reload", async () => {
    const reset = vi.fn();
    render(<PortalErrorPage error={new Error("render blew up")} reset={reset} />);
    expect(screen.getByRole("heading", { name: "WhyGraph hit an unexpected error" })).toBeInTheDocument();
    expect(screen.getByTestId("portal-error")).toHaveTextContent("whygraph logs");
    expect(screen.getByTestId("error-details")).toHaveTextContent("render blew up");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(hardNavigate).toHaveBeenCalled();
  });

  it("gives production no Docker-host advice", () => {
    setErrorMode("production");
    render(<PortalErrorPage error="migration failed" />);
    const page = screen.getByTestId("portal-error");
    expect(page).not.toHaveTextContent("whygraph");
    expect(page).toHaveTextContent("tell your WhyGraph administrator");
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});

// ---- DisabledReason ---------------------------------------------------------------------------

describe("DisabledReason", () => {
  it("shows the reason as text beside the control on touch", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q === "(hover: none)",
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    render(
      <DisabledReason reason="Only project admins can rescan.">
        <button disabled>Rescan</button>
      </DisabledReason>,
    );
    const reason = screen.getByText("Only project admins can rescan.");
    expect(reason).toBeVisible();
    expect(screen.getByRole("button", { name: "Rescan" })).toHaveAttribute("aria-describedby", reason.id);
  });

  it("uses a tooltip and aria-describedby on desktop", async () => {
    render(
      <TooltipProvider>
        <DisabledReason reason="Only project admins can rescan.">
          <button disabled>Rescan</button>
        </DisabledReason>
      </TooltipProvider>,
    );
    const button = screen.getByRole("button", { name: "Rescan" });
    const described = document.getElementById(button.getAttribute("aria-describedby") ?? "");
    expect(described).toHaveTextContent("Only project admins can rescan.");
    expect(described).toHaveClass("sr-only");
    await userEvent.setup().hover(screen.getByTestId("disabled-reason"));
    expect(await screen.findAllByText("Only project admins can rescan.")).toHaveLength(2);
  });
});

// ---- NotFoundPage ------------------------------------------------------------------------------

describe("NotFoundPage", () => {
  it("renders NotFoundState for its kind with one way back", async () => {
    await renderWithRouter(<NotFoundPage kind="project" />);
    expect(screen.getByRole("heading", { name: "Project not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to projects" })).toHaveAttribute("href", "/");
  });

  it("defaults to the plain page kind", async () => {
    await renderWithRouter(<NotFoundPage />);
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
