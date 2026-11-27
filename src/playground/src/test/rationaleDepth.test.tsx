import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DepthInfoButton } from "../components/portal/DepthInfo";
import { OrgCeilingField, RationaleDepthFields } from "../components/portal/RationaleDepthSection";
import { ApiError } from "../api";
import { errorMessage } from "../lib/apiErrors";
import { layerToValues, valuesToLayer, type ConfigFormValues } from "../lib/configForm";
import { ProjectProvider } from "../lib/project";
import { DEPTH_COPY, depthHint, formatCostRange, levelDescription } from "../lib/rationaleDepth";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const level = (cost: { low: number; high: number } | null) => ({
  calls: 1,
  input_tokens: { p50: 10000, p90: 20000 },
  output_tokens: 1500,
  cost,
  first_card: { low: 0.07, high: 0.11 },
});

const ready = (over: Record<string, unknown> = {}) => ({
  status: "ready",
  head: "abc",
  computed_at: "2026-11-27T10:00:00+00:00",
  sampled: 20,
  model: { provider: "anthropic", model: "claude-sonnet-5-5", source: "pinned", price_known: true },
  prices_as_of: "2026-11-11",
  output_source: "default",
  cost_hidden: false,
  levels: {
    low: level({ low: 0.019, high: 0.025 }),
    medium: level({ low: 0.05, high: 0.09 }),
    high: level({ low: 0.03, high: 0.04 }),
  },
  descriptions: { undescribed_share: 0.4, per_commit_usd: 0.015 },
  cheaper_descriptions: { provider: "anthropic", model: "claude-haiku-4-5", per_commit_usd: 0.004, current_per_commit_usd: 0.015 },
  ...over,
});

let answers: Array<{ status?: number; body: unknown }>;

beforeEach(() => {
  answers = [{ body: ready() }];
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      const next = answers.length > 1 ? answers.shift()! : answers[0];
      return Promise.resolve(json(next.body, next.status ?? 200));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function mountPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    render(
      <QueryClientProvider client={client}>
        <ProjectProvider slug="alpha">
          <DepthInfoButton />
        </ProjectProvider>
      </QueryClientProvider>,
    );
  });
  return screen.getByRole("button", { name: "What each depth level includes" });
}

describe("the depth info panel", () => {
  it("opens on click, names its heading, lists every offered level and the footer", async () => {
    const trigger = await mountPanel();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await userEvent.setup().click(trigger);
    const panel = await screen.findByTestId("depth-panel");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(within(panel).getByText(DEPTH_COPY.title)).toBeInTheDocument();
    // The popup is named by its heading (aria-labelledby).
    expect(screen.getByRole("dialog", { name: DEPTH_COPY.title })).toBeInTheDocument();
    for (const name of ["Auto", "Low - Lines", "Medium - Context", "High - Verified"])
      expect(within(panel).getAllByText(name).length).toBeGreaterThan(0);
    // `xhigh` is not offered until Part D.
    expect(within(panel).queryByText(/Extra high/)).toBeNull();
    expect(panel).toHaveTextContent(DEPTH_COPY.footer);
    await waitFor(() => expect(panel).toHaveTextContent("about $0.05-0.09 (estimate)"));
    expect(panel).toHaveTextContent("A commit without a description yet costs about $0.015, once.");
    expect(panel).toHaveTextContent("40% of the sampled commits have no description");
    expect(screen.getByTestId("depth-cheaper")).toHaveTextContent("claude-haiku-4-5");
  });

  it("opens with Enter and closes with Escape, returning focus to the button", async () => {
    const trigger = await mountPanel();
    const user = userEvent.setup();
    await act(async () => trigger.focus());
    await user.keyboard("{Enter}");
    await screen.findByTestId("depth-panel");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("depth-panel")).toBeNull());
    expect(trigger).toHaveFocus();
  });

  it("opens on hover", async () => {
    const trigger = await mountPanel();
    await userEvent.setup().hover(trigger);
    expect(await screen.findByTestId("depth-panel")).toBeInTheDocument();
  });

  it("shows a skeleton row while computing, then the prices", async () => {
    answers = [{ body: { status: "computing" } }, { body: ready() }];
    await userEvent.setup().click(await mountPanel());
    const panel = await screen.findByTestId("depth-panel");
    expect(panel.querySelector("[data-slot='skeleton']")).not.toBeNull();
  });

  it("says when the price is unknown, and when there is no index", async () => {
    answers = [
      {
        body: ready({
          model: { provider: "anthropic", model: null, source: null, price_known: false },
          levels: { low: level(null), medium: level(null), high: level(null) },
          prices_as_of: null,
        }),
      },
    ];
    const user = userEvent.setup();
    await user.click(await mountPanel());
    expect(await screen.findByTestId("depth-price-unknown")).toHaveTextContent(
      "Price unknown until this project pins a rationale model or generates a card.",
    );
  });

  it("says the estimate needs an index", async () => {
    answers = [{ body: { status: "no_index" } }];
    await userEvent.setup().click(await mountPanel());
    expect(await screen.findByTestId("depth-no-index")).toHaveTextContent(
      "The estimate needs a CodeGraph index; run a scan.",
    );
  });

  it("says the estimate needs scanned history", async () => {
    answers = [{ body: { status: "no_history" } }];
    await userEvent.setup().click(await mountPanel());
    expect(await screen.findByTestId("depth-no-history")).toHaveTextContent("The estimate needs scanned history; run a scan.");
  });

  it("shows an error with Retry", async () => {
    answers = [
      { status: 500, body: { error: "boom", code: "depth_profile_failed" } },
      { body: ready() },
    ];
    const user = userEvent.setup();
    await user.click(await mountPanel());
    const panel = await screen.findByTestId("depth-panel");
    await within(panel).findByRole("button", { name: "Retry" });
    await user.click(within(panel).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(panel).toHaveTextContent("about $0.05-0.09 (estimate)"));
  });
});

describe("the depth copy", () => {
  it("is one constant: the select's hint is the panel's row", () => {
    expect(depthHint("low")).toBe(levelDescription("low"));
    expect(depthHint("auto")).toBe(DEPTH_COPY.auto.description);
  });

  it("formats a range once, and one figure when both ends read alike", () => {
    expect(formatCostRange({ low: 0.05, high: 0.09 })).toBe("$0.05-0.09");
    expect(formatCostRange({ low: 0.019, high: 0.019 })).toBe("$0.019");
    expect(formatCostRange(null)).toBeNull();
  });
});

function Harness({ values, orgCeiling }: { values: Partial<ConfigFormValues>; orgCeiling: string | null }) {
  const form = useForm<ConfigFormValues>({ defaultValues: { ...layerToValues({}), ...values } });
  return (
    <ProjectProvider slug="alpha">
      <RationaleDepthFields form={form} readOnly={false} inheritedDepth="auto" inheritedMax="high" orgCeiling={orgCeiling} />
    </ProjectProvider>
  );
}

function mountFields(values: Partial<ConfigFormValues>, orgCeiling: string | null) {
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <Harness values={values} orgCeiling={orgCeiling} />
    </QueryClientProvider>,
  );
}

describe("the Rationale depth fields", () => {
  it("disables the depth options above the ceiling and labels them", () => {
    mountFields({ maxDepth: "medium" }, null);
    const depth = screen.getByLabelText("Depth") as HTMLSelectElement;
    const options = Array.from(depth.options).map((o) => [o.textContent, o.disabled]);
    expect(options).toEqual([
      ["Auto", false],
      ["Low - Lines", false],
      ["Medium - Context", false],
      ["High - Verified (above ceiling)", true],
    ]);
  });

  it("follows the selection with its hint and shows the org's ceiling", async () => {
    mountFields({ maxDepth: "medium" }, "medium");
    const depth = screen.getByLabelText("Depth");
    expect(screen.getByText(DEPTH_COPY.auto.description)).toBeInTheDocument();
    await userEvent.setup().selectOptions(depth, "low");
    expect(screen.getByText(levelDescription("low"))).toBeInTheDocument();
    // The org allows up to Medium: High is not a choice for the project's ceiling.
    expect(screen.getByText(/Your organization allows up to Medium\./)).toBeInTheDocument();
    const ceiling = screen.getByLabelText("Ceiling") as HTMLSelectElement;
    expect(Array.from(ceiling.options).find((o) => o.value === "high")?.disabled).toBe(true);
  });

  it("says a stored ceiling above the org's is capped", () => {
    mountFields({ maxDepth: "high" }, "low");
    expect(screen.getByText(/Capped by your organization at Low\./)).toBeInTheDocument();
  });

  it("has the org ceiling select with its hint", () => {
    function Org() {
      const form = useForm<ConfigFormValues>({ defaultValues: layerToValues({}) });
      return <OrgCeilingField form={form} readOnly={false} />;
    }
    render(<Org />);
    expect(screen.getByLabelText("Organization ceiling")).toHaveValue("high");
    expect(screen.getByText(/never a higher one/)).toBeInTheDocument();
  });
});

describe("the depth layer", () => {
  it("reads and writes depth, max_depth and org_max_depth beside the other [rationale] keys", () => {
    const base = { rationale: { model: "m", agent_generations_per_hour: 5 } };
    const values = { ...layerToValues(base), depth: "medium", maxDepth: "high", orgMaxDepth: "medium" };
    // Only the section that owns a key writes it.
    expect(valuesToLayer(base, values, { scan: true }).rationale).toEqual(base.rationale);
    expect(valuesToLayer(base, values, { scan: true, depth: true }).rationale).toEqual({
      ...base.rationale,
      depth: "medium",
      max_depth: "high",
    });
    expect(valuesToLayer(base, values, { scan: false, ceiling: true }).rationale).toEqual({
      ...base.rationale,
      org_max_depth: "medium",
    });
    expect(layerToValues({ rationale: { depth: "low", max_depth: "medium" } })).toMatchObject({
      depth: "low",
      maxDepth: "medium",
      orgMaxDepth: "",
    });
  });

  it("words depth_above_ceiling with the ceiling the server names", () => {
    const err = new ApiError(422, "above", "depth_above_ceiling", { ceiling: "high" });
    expect(errorMessage(err)).toBe("Above your organization's ceiling (High).");
  });
});
