import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Markdown } from "../components/chat/Markdown";
import { ThemeToggle } from "../components/ThemeToggle";
import { STORAGE_KEY, ThemeProvider, useTheme } from "../theme";

const root = document.documentElement;
const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

/** A controllable `prefers-color-scheme: dark` media query. */
function mockSystem(dark: boolean) {
  let matches = dark;
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return matches;
      },
      addEventListener: (_: string, l: (e: MediaQueryListEvent) => void) => listeners.add(l),
      removeEventListener: (_: string, l: (e: MediaQueryListEvent) => void) => listeners.delete(l),
    })),
  );
  return (next: boolean) => {
    matches = next;
    listeners.forEach((l) => l({ matches: next } as MediaQueryListEvent));
  };
}

function Probe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="resolved">{resolvedTheme}</span>
      <button onClick={() => setTheme("dark")}>pin dark</button>
      <button onClick={() => setTheme("light")}>pin light</button>
      <button onClick={() => setTheme("system")}>use system</button>
    </div>
  );
}

const mount = () =>
  render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  );

beforeEach(() => {
  localStorage.clear();
  root.classList.remove("dark");
  root.style.colorScheme = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ThemeProvider", () => {
  it("follows a light system by default", () => {
    mockSystem(false);
    mount();
    expect(screen.getByTestId("theme")).toHaveTextContent("system");
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
    expect(root).not.toHaveClass("dark");
  });

  it("follows a dark system by default", () => {
    mockSystem(true);
    mount();
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    expect(root).toHaveClass("dark");
    expect(root.style.colorScheme).toBe("dark");
  });

  it("tracks the system live while set to system", () => {
    const setSystem = mockSystem(false);
    mount();
    act(() => setSystem(true));
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    expect(root).toHaveClass("dark");
    act(() => setSystem(false));
    expect(root).not.toHaveClass("dark");
  });

  it("pins a theme, persists it, and ignores the system afterwards", async () => {
    const setSystem = mockSystem(false);
    mount();
    await userEvent.click(screen.getByText("pin dark"));
    expect(root).toHaveClass("dark");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("dark");
    act(() => setSystem(false));
    expect(screen.getByTestId("resolved")).toHaveTextContent("dark");
    await userEvent.click(screen.getByText("use system"));
    expect(screen.getByTestId("resolved")).toHaveTextContent("light");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("system");
  });

  it("restores a stored choice over the system", () => {
    mockSystem(true);
    localStorage.setItem(STORAGE_KEY, "light");
    mount();
    expect(screen.getByTestId("theme")).toHaveTextContent("light");
    expect(root).not.toHaveClass("dark");
  });

  it("ignores a garbage stored value", () => {
    mockSystem(false);
    localStorage.setItem(STORAGE_KEY, "solarized");
    mount();
    expect(screen.getByTestId("theme")).toHaveTextContent("system");
  });

  it("survives storage that throws", async () => {
    mockSystem(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    mount();
    await userEvent.click(screen.getByText("pin dark"));
    expect(root).toHaveClass("dark");
  });

  it("useTheme outside the provider fails loudly", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => render(<Probe />)).toThrow(/ThemeProvider/);
  });
});

describe("ThemeToggle", () => {
  it("cycles system, light, dark", async () => {
    mockSystem(false);
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    const button = screen.getByRole("button");
    expect(button).toHaveTextContent("System");
    await userEvent.click(button);
    expect(button).toHaveTextContent("Light");
    await userEvent.click(button);
    expect(button).toHaveTextContent("Dark");
    expect(root).toHaveClass("dark");
    await userEvent.click(button);
    expect(button).toHaveTextContent("System");
  });
});

describe("chat markdown in both themes", () => {
  it.each(["light", "dark"] as const)("renders typography in the %s theme", (theme) => {
    mockSystem(false);
    localStorage.setItem(STORAGE_KEY, theme);
    const { container } = render(
      <ThemeProvider>
        <Markdown>{"## Heading\n\nSome **bold** text and `code`.\n\n- one\n- two"}</Markdown>
      </ThemeProvider>,
    );
    const prose = container.querySelector(".prose");
    expect(prose).not.toBeNull();
    // Colours come from the token-driven --tw-prose-* values, never `prose-invert`.
    expect(prose).not.toHaveClass("prose-invert");
    expect(screen.getByRole("heading", { name: "Heading" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(root.classList.contains("dark")).toBe(theme === "dark");
  });
});

describe("design tokens", () => {
  const css = read("../styles/theme.css");
  const block = (selector: string) => {
    const start = css.indexOf(`\n${selector} {`);
    return css.slice(start, css.indexOf("\n}", start));
  };
  const names = (body: string, literalOnly: boolean) =>
    [...body.matchAll(/^\s*(--[\w-]+):\s*([^;]+);/gm)]
      .filter(([, , value]) => !literalOnly || !value.trim().startsWith("var("))
      .map(([, name]) => name);

  it("gives every literal light token a dark counterpart", () => {
    const light = names(block(":root"), true).filter((n) => n !== "--radius");
    const dark = names(block(".dark"), false);
    expect(light.length).toBeGreaterThan(10);
    expect(light.filter((n) => !dark.includes(n))).toEqual([]);
  });

  it("uses OKLCH for colours", () => {
    // `--card-shadow` is a raw box-shadow, not a colour (its colours are OKLCH inside).
    for (const selector of [":root", ".dark"]) {
      const literals = [...block(selector).matchAll(/^\s*(--[\w-]+):\s*([^;]+);/gm)]
        .filter(([, name]) => name !== "--card-shadow")
        .map(([, , v]) => v.trim())
        .filter((v) => !v.startsWith("var(") && !v.endsWith("rem") && v !== "light" && v !== "dark");
      expect(literals.every((v) => v.startsWith("oklch("))).toBe(true);
    }
    expect(block(":root")).toContain("--card-shadow: ");
    expect(block(".dark")).toContain("--card-shadow: ");
  });

  it("defines the status, soft, info and track tokens in both themes", () => {
    for (const selector of [":root", ".dark"]) {
      const defined = names(block(selector), false);
      for (const token of [
        "--primary-soft",
        "--success-soft",
        "--warning-soft",
        "--destructive-soft",
        "--info",
        "--info-soft",
        "--info-foreground",
        "--track",
      ]) {
        expect(defined).toContain(token);
      }
    }
    expect(css).toContain("--shadow-card: var(--card-shadow)");
    expect(css).toContain("@utility row-wrap");
  });

  it("has no legacy alias layer left", () => {
    for (const legacy of ["bg", "panel", "panel2", "fg", "accent2"]) {
      expect(css).not.toMatch(new RegExp(`--color-${legacy}:`));
    }
  });
});

describe("pre-paint script in index.html", () => {
  const html = read("../../index.html");
  const script = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1] ?? "";

  it("is present and uses the same storage key as the provider", () => {
    expect(script).toContain(`"${STORAGE_KEY}"`);
  });

  it.each([
    ["system", true, true],
    ["system", false, false],
    ["dark", false, true],
    ["light", true, false],
  ] as const)("stored %s + system dark=%s -> dark class %s", (stored, systemDark, expected) => {
    mockSystem(systemDark);
    localStorage.setItem(STORAGE_KEY, stored);
    new Function(script)();
    expect(root.classList.contains("dark")).toBe(expected);
    expect(root.style.colorScheme).toBe(expected ? "dark" : "light");
  });

  it("does not throw when storage does", () => {
    mockSystem(true);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => new Function(script)()).not.toThrow();
    expect(root).toHaveClass("dark");
  });
});
