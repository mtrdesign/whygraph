import { Fragment, useLayoutEffect, useRef, useState } from "react";
import { cn } from "cn";
import { CopyButton } from "../portal/CopyButton";

/**
 * The path as printed: relative to the shared folder that contains it when one of
 * `base` does, otherwise the whole path.
 */
export function displayPath(path: string, base?: string[]): string {
  for (const b of base ?? []) {
    const root = b.replace(/\/+$/, "");
    if (!root) continue;
    if (path === root) return root.split("/").pop() || root;
    if (path.startsWith(`${root}/`)) return path.slice(root.length + 1);
  }
  return path;
}

/**
 * True when the path as printed (see `displayPath`) only repeats the project's
 * name or slug: a repo directly in a shared folder. A subtitle that says nothing
 * more is hidden (the full path stays in Settings and the tooltips elsewhere).
 */
export function pathRepeatsName(path: string, base: string[] | undefined, project: { name: string; slug: string }): boolean {
  const shown = displayPath(path, base).toLowerCase();
  return shown === project.name.toLowerCase() || shown === project.slug.toLowerCase();
}

/** Keep the tail of `text` in at most `max` characters, led by an ellipsis. */
export function truncateStart(text: string, max: number): string {
  if (max < 2 || text.length <= max) return text;
  return `…${text.slice(text.length - (max - 1))}`;
}

/** `shown` with a break opportunity after each `/`, so a block wraps between segments. */
function withBreaks(shown: string) {
  return shown.split(/(?<=\/)/).map((part, i) => (
    <Fragment key={i}>
      {i > 0 && <wbr />}
      {part}
    </Fragment>
  ));
}

/**
 * A filesystem path in mono. `inline` is one line truncated from the start (the
 * distinguishing tail stays), measured in JS rather than with `direction: rtl`;
 * the full path is in the tooltip and `aria-label`. `block` wraps after a `/`
 * (a segment splits only when it alone is wider than the line).
 */
export function PathText({
  path,
  base,
  copy = false,
  variant = "inline",
  className,
}: {
  path: string;
  base?: string[];
  copy?: boolean;
  variant?: "inline" | "block";
  className?: string;
}) {
  const shown = displayPath(path, base);
  const ref = useRef<HTMLSpanElement>(null);
  const [text, setText] = useState(shown);

  useLayoutEffect(() => {
    setText(shown);
    if (variant !== "inline") return;
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const fit = () => {
      // Measure with the full text in place, then cut to what fits.
      el.textContent = shown;
      const avail = el.clientWidth;
      const full = el.scrollWidth;
      if (avail > 0 && full > avail) {
        const perChar = full / shown.length;
        setText(truncateStart(shown, Math.max(2, Math.floor(avail / perChar))));
      } else {
        setText(shown);
      }
    };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [shown, variant]);

  const span = (
    <span
      ref={ref}
      title={path}
      aria-label={path}
      data-slot="path-text"
      className={cn(
        "font-mono text-xs",
        variant === "inline" ? "block min-w-0 flex-1 truncate whitespace-nowrap" : "wrap-anywhere",
        className,
      )}
    >
      {variant === "inline" ? text : withBreaks(shown)}
    </span>
  );
  if (!copy) return span;
  return (
    <span className="flex min-w-0 items-center gap-2">
      {span}
      <span className="shrink-0">
        <CopyButton text={path} variant="ghost" />
      </span>
    </span>
  );
}
