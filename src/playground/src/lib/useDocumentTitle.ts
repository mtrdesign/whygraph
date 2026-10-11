import { useEffect } from "react";

/** "<page> · <project> · WhyGraph", without empty or repeated parts. */
export function documentTitle(page?: string, project?: string): string {
  const parts = [page, project && project !== page ? project : undefined, "WhyGraph"];
  return parts.filter((p): p is string => !!p).join(" · ");
}

/**
 * Set `document.title` to "<page> · <project> · WhyGraph" (NAV-4), so a tab, the
 * history menu and a screen reader's page announcement name the page.
 */
export function useDocumentTitle(page?: string, project?: string): void {
  const title = documentTitle(page, project);
  useEffect(() => {
    document.title = title;
  }, [title]);
}
