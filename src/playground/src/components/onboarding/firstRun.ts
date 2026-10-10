import { useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { onboardingApi, portalKey } from "../../api";
import { usePortalState } from "../../lib/identity";

/** localStorage key of the dismissal: one per organization and person (§0.3 #24). */
export function dismissKey(org: string, uid: string): string {
  return `whygraph.firstRun.dismissed.${org}.${uid}`;
}

function read(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

/**
 * The first-run checklist's data: fetched only when `enabled` (the caller holds
 * `org.add_project`), dismissed per browser, and "open" while an item is undone.
 */
export function useFirstRun(enabled: boolean) {
  const state = usePortalState().data;
  const key = dismissKey(state?.org?.slug ?? "local", state?.user?.uid ?? "-");
  const [dismissedKey, setDismissed] = useState<string | null>(() => (read(key) ? key : null));
  const query = useQuery({
    queryKey: portalKey("onboarding"),
    queryFn: onboardingApi.onboarding,
    enabled,
    retry: false,
  });
  const dismissed = dismissedKey === key || (dismissedKey === null && read(key));
  const items = query.data?.items ?? [];
  const open = items.some((i) => !i.done);
  const dismiss = useCallback(() => {
    try {
      window.localStorage.setItem(key, "1");
    } catch {
      // a convenience only: hidden for this visit
    }
    setDismissed(key);
  }, [key]);
  const restore = useCallback(() => {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // nothing stored
    }
    setDismissed("");
  }, [key]);
  return { items, open, dismissed, dismiss, restore, isLoading: query.isLoading, isError: query.isError };
}

export type FirstRun = ReturnType<typeof useFirstRun>;
