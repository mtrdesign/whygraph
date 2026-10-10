import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PathText, displayPath, truncateStart } from "../components/layout/PathText";
import { repoRowBase } from "../components/portal/LocalSource";
import { ResponsiveTable } from "../components/layout/ResponsiveTable";
import { TabsSelect } from "../components/layout/TabsSelect";
import { CommandBlock } from "../components/layout/CommandBlock";
import { PageContainer } from "../components/layout/PageContainer";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { LiveRegion, announce } from "../components/shell/LiveRegion";
import { nativeSelect, nativeSelectClass } from "../components/portal/Field";
import { useIsPhone } from "../lib/useIsPhone";
import { formatDate, formatDateTime, formatNumber, formatRelative, timeAgo } from "../lib/format";
import { timeAgo as reTimeAgo } from "../lib/projectStatus";
import { triggerLabel as reTrigger } from "../lib/scanFormat";
import * as labels from "../lib/labels";

afterEach(() => vi.unstubAllGlobals());

describe("PathText", () => {
  it("is relative to the containing shared folder", () => {
    expect(displayPath("/home/me/repos/app", ["/home/me/repos"])).toBe("app");
    expect(displayPath("/elsewhere/app", ["/home/me/repos"])).toBe("/elsewhere/app");
  });
  it("truncates from the start, keeping the tail", () => {
    expect(truncateStart("/a/very/long/path/tail", 10)).toBe("…path/tail".slice(0, 1) + "/path/tail".slice(-9));
    expect(truncateStart("short", 10)).toBe("short");
  });
  it("carries the full path in the tooltip and aria-label", () => {
    render(<PathText path="/a/b/c" base={["/a"]} />);
    const el = screen.getByLabelText("/a/b/c");
    expect(el).toHaveAttribute("title", "/a/b/c");
    expect(el).toHaveTextContent("b/c");
  });
  it("keeps the start-truncated tail of every sibling row after the resize observer fires", async () => {
    // jsdom has no layout: the span is 140px wide and each mono character 7px.
    const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
    const scroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollWidth");
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get(this: HTMLElement) {
        return this.dataset.slot === "path-text" ? 140 : 0;
      },
    });
    Object.defineProperty(Element.prototype, "scrollWidth", {
      configurable: true,
      get(this: Element) {
        return (this.textContent ?? "").length * 7;
      },
    });
    const callbacks: (() => void)[] = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(private cb: () => void) {}
        observe() {
          callbacks.push(this.cb);
        }
        disconnect() {}
      },
    );
    try {
      const names = ["billing", "notes", "payments", "search", "web"];
      render(
        <div>
          {names.map((n) => (
            <PathText key={n} path={`/private/var/folders/56/abcdef/T/shared/${n}`} />
          ))}
        </div>,
      );
      // The observer's first callback lands right after the first fit.
      await act(async () => callbacks.forEach((cb) => cb()));
      const shown = names.map((n) => screen.getByLabelText(`/private/var/folders/56/abcdef/T/shared/${n}`).textContent);
      shown.forEach((t, i) => {
        expect(t!.startsWith("…")).toBe(true);
        expect(t!.endsWith(`/shared/${names[i]}`)).toBe(true);
        expect(t!.length).toBeLessThanOrEqual(20);
      });
      expect(new Set(shown).size).toBe(names.length);
    } finally {
      if (width) Object.defineProperty(HTMLElement.prototype, "clientWidth", width);
      if (scroll) Object.defineProperty(Element.prototype, "scrollWidth", scroll);
    }
  });
  it("an add-project repo row reads <shared folder>/<name> when the relative path only repeats the name (A8)", () => {
    const shared = ["/private/var/folders/56/T/shared"];
    const row = (path: string, name: string) => displayPath(path, repoRowBase(path, name, shared));
    expect(row("/private/var/folders/56/T/shared/billing", "billing")).toBe("shared/billing");
    // Deeper: the path relative to the shared folder already says more than the name.
    expect(row("/private/var/folders/56/T/shared/team/billing", "billing")).toBe("team/billing");
    // The shared folder itself is the repo: its parent's name leads.
    expect(row("/private/var/folders/56/T/shared", "shared")).toBe("T/shared");
    // Outside every shared folder: the whole path.
    expect(row("/elsewhere/billing", "billing")).toBe("/elsewhere/billing");
  });
  it("wraps in block variant between segments, never with break-all", () => {
    render(<PathText path="/a/b" variant="block" />);
    const el = screen.getByLabelText("/a/b");
    expect(el.className).toContain("wrap-anywhere");
    expect(el.className).not.toContain("break-all");
    expect(el.textContent).toBe("/a/b");
    expect(el.querySelectorAll("wbr")).toHaveLength(2);
  });
});

describe("CommandBlock / PageContainer", () => {
  it("marks the block as an intentional scroller and shows the exact command", () => {
    const { container } = render(<CommandBlock command="whygraph up --add-folder /x" />);
    const pre = container.querySelector("pre")!;
    expect(pre).toHaveAttribute("data-scroll-x");
    expect(pre.textContent).toBe("whygraph up --add-folder /x");
  });
  it("applies the width", () => {
    const { container } = render(<PageContainer width="narrow">x</PageContainer>);
    expect(container.firstElementChild!.className).toContain("max-w-3xl");
  });
});

describe("ResponsiveTable", () => {
  const rows = [{ id: "1", name: "alpha", n: 3 }];
  const columns = [
    { key: "n", header: "Count", cell: (r: (typeof rows)[0]) => r.n },
    { key: "name", header: "Name", cell: (r: (typeof rows)[0]) => r.name, primary: true },
  ];
  it("renders a table and a stacked list with the primary column as title", () => {
    const { container } = render(
      <ResponsiveTable columns={columns} rows={rows} rowKey={(r) => r.id} />,
    );
    expect(container.querySelector("table")).toBeTruthy();
    expect(container.querySelector("li .font-medium")).toHaveTextContent("alpha");
    expect(container.querySelector("li dt")).toHaveTextContent("Count");
  });
  it("renders the empty state and handles row clicks", () => {
    const onRow = vi.fn();
    const { rerender, container } = render(
      <ResponsiveTable columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={onRow} />,
    );
    fireEvent.click(container.querySelector("tbody tr")!);
    expect(onRow).toHaveBeenCalledWith(rows[0]);
    rerender(
      <ResponsiveTable columns={columns} rows={[]} rowKey={(r) => r.id} empty={<p>None</p>} />,
    );
    expect(screen.getByText("None")).toBeInTheDocument();
  });
});

describe("tabs", () => {
  it("scrollable variant is an intentional horizontal scroller", () => {
    const { container } = render(
      <Tabs defaultValue="a">
        <TabsList variant="scrollable">
          <TabsTrigger value="a">A</TabsTrigger>
          <TabsTrigger value="b">B</TabsTrigger>
        </TabsList>
      </Tabs>,
    );
    const list = container.querySelector("[data-slot=tabs-list]")!;
    expect(list).toHaveAttribute("data-scroll-x");
    expect(list.className).toContain("overflow-x-auto");
  });
  it("TabsSelect reports the chosen tab", () => {
    const on = vi.fn();
    render(
      <TabsSelect
        label="Section"
        value="a"
        onValueChange={on}
        tabs={[
          { value: "a", label: "A" },
          { value: "b", label: "B" },
        ]}
      />,
    );
    fireEvent.change(screen.getByLabelText("Section"), { target: { value: "b" } });
    expect(on).toHaveBeenCalledWith("b");
  });
});

describe("nativeSelect", () => {
  it("keeps w-full by default and lets a later width win", () => {
    expect(nativeSelectClass).toContain("w-full");
    const c = nativeSelect("w-36");
    expect(c).toContain("w-36");
    expect(c).not.toContain("w-full");
  });
});

describe("useIsPhone", () => {
  it("is false without matchMedia", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(renderHook(() => useIsPhone()).result.current).toBe(false);
  });
  it("follows the query", () => {
    vi.stubGlobal("matchMedia", (q: string) => ({
      matches: q.includes("767"),
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    expect(renderHook(() => useIsPhone()).result.current).toBe(true);
  });
});

describe("LiveRegion", () => {
  it("announces politely", () => {
    render(<LiveRegion />);
    act(() => announce("Saved"));
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region.textContent).toContain("Saved");
  });
});

describe("format", () => {
  it("formats the parts, not a locale string", () => {
    const iso = "2026-10-09T15:04:00Z";
    expect(formatDate(iso)).toContain("2026");
    expect(formatDateTime(iso)).toContain("2026");
    expect(formatDateTime(iso).length).toBeGreaterThan(formatDate(iso).length);
    expect(formatDate(null)).toBe("-");
    expect(formatDate("nope")).toBe("-");
    expect(formatNumber(1234567)).toMatch(/1\D?234\D?567/);
    expect(formatNumber(null)).toBe("-");
  });
  it("formats relative time and keeps the old export", () => {
    const now = Date.parse("2026-10-09T12:00:00Z");
    expect(formatRelative("2026-10-09T11:57:00Z", now)).toBe("3 min ago");
    expect(formatRelative(undefined)).toBe("-");
    expect(reTimeAgo).toBe(timeAgo);
  });
});

describe("labels", () => {
  it("names roles, agents, providers and sources", () => {
    expect(labels.orgRoleLabel("owner")).toBe("Owner");
    expect(labels.projectRoleLabel("contributor")).toBe("Contributor");
    expect(labels.agentLabel("claude")).toBe("Claude Code");
    expect(labels.providerLabel("openai")).toBe("OpenAI");
    expect(labels.sourceLabel("github")).toBe("GitHub");
    expect(labels.sourceLabel("odd")).toBe("odd");
  });
  it("names the inherited layer by mode", () => {
    expect(labels.inheritedLayerLabel("local")).toBe("Portal defaults");
    expect(labels.inheritedLayerLabel("local", true)).toBe("Portal");
    expect(labels.inheritedLayerLabel("production")).toBe("Organization");
  });
  it("keeps triggerLabel importable from scanFormat", () => {
    expect(reTrigger).toBe(labels.triggerLabel);
    expect(labels.triggerLabel({ trigger: "hook", kind: "scan" } as never)).toBe("Git hook");
    expect(labels.triggerLabel({ trigger: "x", kind: "sync" } as never)).toBe("Sync");
  });
});
