import { useEffect } from "react";
import { useRouter } from "@tanstack/react-router";
import { announce } from "./LiveRegion";

// How long a page may take to render its heading (a skeleton first, then the h1)
// before focus settles on the main region instead.
const ATTEMPTS = 10;
const STEP_MS = 50;

/** The page's heading inside the main region, else the first one on the page. */
function pageHeading(main: HTMLElement | null): HTMLElement | null {
  return (main?.querySelector("h1") ?? document.querySelector("h1")) as HTMLElement | null;
}

/** True when the new page already put focus on one of its own controls (an autofocused field). */
function pageChoseFocus(main: HTMLElement | null): boolean {
  const active = document.activeElement;
  return !!main && !!active && active !== main && main.contains(active) && active.tagName !== "H1";
}

/**
 * Focus on navigation (NAV-3): when the path changes (never for a search-only
 * change such as an Explorer selection or a filter), move focus to the new page's
 * `h1` (else `<main id="main">`) and announce the page title through the live
 * region. Mounted once at the root, so it survives the shell remounting between
 * the portal and project layouts.
 */
export function RouteFocus() {
  const router = useRouter();
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unsubscribe = router.subscribe("onResolved", (event) => {
      if (!event.pathChanged || !event.fromLocation) return;
      if (timer) clearTimeout(timer);
      const settle = (attempt: number) => {
        const main = document.getElementById("main");
        const heading = pageHeading(main);
        if (!heading && attempt < ATTEMPTS) {
          timer = setTimeout(() => settle(attempt + 1), STEP_MS);
          return;
        }
        if (!pageChoseFocus(main)) {
          const target = heading ?? main;
          if (target) {
            if (target === heading && !heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
            target.focus({ preventScroll: true });
          }
        }
        const title = document.title.replace(/ · WhyGraph$/, "");
        if (title) announce(title);
      };
      timer = setTimeout(() => settle(0), STEP_MS);
    });
    return () => {
      if (timer) clearTimeout(timer);
      unsubscribe();
    };
  }, [router]);
  return null;
}
