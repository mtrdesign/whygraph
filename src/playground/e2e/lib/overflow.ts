import type { Page } from "@playwright/test";

/**
 * Where the page overflows horizontally at the current viewport, as short
 * descriptions (at most six; empty means none). Shared by the screenshot audit
 * (`audit/lib/shoot.ts`) and the phone spec (`tests/phone.spec.ts`).
 *
 * It looks at the document, at every scroller inside `main` (an element whose
 * own content is wider than its box), and at the portaled UI - dialogs, sheets
 * and menus - whose content, or whose own box, leaves the viewport. An element
 * marked `data-scroll-x` (and anything inside one) is an opted-in scroller and
 * is never reported.
 */
export async function horizontalOverflow(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const vw = window.innerWidth;
    const label = (el: Element) => {
      const id = el.getAttribute("data-testid") ?? el.getAttribute("data-slot") ?? el.getAttribute("role");
      return id ? `${el.tagName.toLowerCase()}[${id}]` : el.tagName.toLowerCase();
    };
    const exempt = (el: Element) => el.closest("[data-scroll-x]") !== null;
    const shown = (el: HTMLElement) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
    };

    const doc = document.documentElement;
    if (doc.scrollWidth > vw + 1) out.push(`document ${doc.scrollWidth}px in ${vw}px`);

    const scrollers = document.querySelectorAll<HTMLElement>("main, main *");
    for (const el of Array.from(scrollers)) {
      if (out.length >= 6) break;
      if (exempt(el) || !shown(el)) continue;
      const ox = getComputedStyle(el).overflowX;
      if ((ox === "auto" || ox === "scroll") && el.scrollWidth > el.clientWidth + 1) {
        out.push(`${label(el)} scrolls ${el.scrollWidth}px in ${el.clientWidth}px`);
      }
    }

    const portals = document.querySelectorAll<HTMLElement>(
      '[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"], [data-slot="sheet-content"], [data-slot="dialog-content"], [data-slot="popover-content"]',
    );
    for (const root of Array.from(portals)) {
      if (out.length >= 6) break;
      if (exempt(root) || !shown(root)) continue;
      const box = root.getBoundingClientRect();
      if (box.right > vw + 1 || box.left < -1) {
        out.push(`${label(root)} spans ${Math.round(box.left)}..${Math.round(box.right)}px in ${vw}px`);
        continue;
      }
      if (root.scrollWidth > root.clientWidth + 1) {
        out.push(`${label(root)} scrolls ${root.scrollWidth}px in ${root.clientWidth}px`);
        continue;
      }
      for (const el of Array.from(root.querySelectorAll<HTMLElement>("*"))) {
        if (exempt(el) || !shown(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.right > vw + 1) {
          out.push(`${label(root)} > ${label(el)} reaches ${Math.round(r.right)}px in ${vw}px`);
          break;
        }
      }
    }
    return out;
  });
}
