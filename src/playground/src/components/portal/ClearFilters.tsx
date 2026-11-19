import { cn } from "../../lib/utils";
import { Button } from "../ui/button";

/**
 * "Clear filters": the link-style button at the end of a filter bar that puts every control of the bar
 * (search text, selects, sort, dates) back to its default in one click. A page renders it only while
 * something differs from the default.
 */
export function ClearFilters({ onClear, className }: { onClear: () => void; className?: string }) {
  return (
    <Button
      type="button"
      variant="link"
      className={cn("px-0 text-primary-text", className)}
      onClick={onClear}
      data-testid="clear-filters"
    >
      Clear filters
    </Button>
  );
}
