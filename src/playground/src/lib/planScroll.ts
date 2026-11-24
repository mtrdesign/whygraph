import type { PlanOutlineEntry } from "../api";

/**
 * The element a plan outline entry `n` opens at, inside the rendered plan body (R19).
 *
 * The outline holds no offsets, so the match is by order: the k-th `##` / `###` heading the
 * Markdown renders is the k-th outline entry that is not entry 0, not a `(continued ...)`
 * part and not a step entry (the outline skips fenced code exactly as Markdown does). A step
 * entry (`Step <k>: ...`) is the k-th top-level numbered item under the `Steps` heading; a
 * continuation part opens at its first part's anchor. A number that matches nothing gives
 * `null` and the page stays at the top.
 */
export function planAnchor(root: HTMLElement, outline: PlanOutlineEntry[], n: number): HTMLElement | null {
  const entry = outline.find((e) => e.n === n);
  if (!entry || entry.n === 0) return null;
  if (entry.step !== null) return stepItem(root, entry.step);

  const headings = Array.from(root.querySelectorAll<HTMLElement>("h2, h3"));
  const counted = outline.filter((e) => e.n > 0 && e.step === null && !(e.part && e.part[0] > 1));
  let target = entry;
  if (entry.part && entry.part[0] > 1) {
    // The first part of the same section: the nearest earlier entry with part 1.
    const first = [...counted].reverse().find((e) => e.n < entry.n && e.part?.[0] === 1);
    if (!first) return null;
    target = first;
  }
  const index = counted.findIndex((e) => e.n === target.n);
  return index >= 0 ? (headings[index] ?? null) : null;
}

function stepItem(root: HTMLElement, k: number): HTMLElement | null {
  const heading = Array.from(root.querySelectorAll<HTMLElement>("h2")).find(
    (h) => (h.textContent ?? "").trim().toLowerCase() === "steps",
  );
  if (!heading) return null;
  let node = heading.nextElementSibling;
  while (node && node.tagName !== "H2") {
    if (node.tagName === "OL") {
      const items = Array.from(node.children).filter((c): c is HTMLElement => c.tagName === "LI");
      return items[k - 1] ?? null;
    }
    node = node.nextElementSibling;
  }
  return null;
}
