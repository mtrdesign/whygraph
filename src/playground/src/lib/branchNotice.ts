import type { LastScanStats } from "../api";
import { plural } from "./plural";

export type BranchStatus = NonNullable<LastScanStats["branch"]>;

/**
 * The "scanned on an older branch" notice (plan 64), or `null` when there is
 * nothing to say: the default branch did not resolve, the checkout is not behind
 * it, or the checkout is the default branch. A detached HEAD (`current` null)
 * counts as another branch.
 */
export function checkoutNotice(b: BranchStatus | null | undefined): { title: string; detail: string } | null {
  if (!b || !b.default || !b.behind || b.behind <= 0 || b.current === b.default) return null;
  const where = b.current ? `On ${b.current}` : "On a detached HEAD";
  return {
    title: `${where}, ${plural(b.behind, "commit")} behind ${b.default}`,
    detail: `History includes all of ${b.default}. The code index, core files and plans are from your checkout until you switch; WhyGraph rescans when you do. Counted as of your last fetch.`,
  };
}
