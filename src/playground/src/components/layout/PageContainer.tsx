import type { ReactNode } from "react";
import { cn } from "cn";

const WIDTH = {
  narrow: "max-w-3xl",
  default: "max-w-5xl",
  wide: "max-w-7xl",
  full: "",
} as const;

/**
 * The one page wrapper: centred, with a phone-safe gutter. Every page, and every
 * loading / error state, renders inside it; Explorer and Chat use `full`.
 */
export function PageContainer({
  width = "default",
  className,
  children,
}: {
  width?: keyof typeof WIDTH;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("mx-auto w-full px-4 py-5 sm:px-6", WIDTH[width], className)}>
      {children}
    </div>
  );
}
