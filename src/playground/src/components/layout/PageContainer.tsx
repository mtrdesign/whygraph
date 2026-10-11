import { createContext, useContext, type ReactNode } from "react";
import { cn } from "cn";

const WIDTH = {
  narrow: "max-w-3xl",
  default: "max-w-5xl",
  wide: "max-w-7xl",
  full: "",
} as const;

export type PageWidth = keyof typeof WIDTH;

const InsidePage = createContext(false);

/** True under a `PageContainer`: a page-level state then renders bare, at the page's position. */
export function useInsidePageContainer(): boolean {
  return useContext(InsidePage);
}

/**
 * The one page wrapper: centred, with a phone-safe gutter. Every page, and every
 * loading / error state, renders inside it; Explorer and Chat use `full`.
 */
export function PageContainer({
  width = "default",
  className,
  children,
  "data-testid": testId,
}: {
  width?: PageWidth;
  className?: string;
  children: ReactNode;
  "data-testid"?: string;
}) {
  return (
    <InsidePage.Provider value={true}>
      <div className={cn("mx-auto w-full px-4 py-5 sm:px-6", WIDTH[width], className)} data-testid={testId}>
        {children}
      </div>
    </InsidePage.Provider>
  );
}

/**
 * `children` in a `PageContainer` unless one already holds them: a page-level
 * state (not found, a failed load) adds no second box of its own.
 */
export function InPage({ width = "default", children }: { width?: PageWidth; children: ReactNode }) {
  const inside = useInsidePageContainer();
  return inside ? <>{children}</> : <PageContainer width={width}>{children}</PageContainer>;
}
