import { createContext, useContext, type ReactNode } from "react";
import { NetworkIcon } from "lucide-react";

/** True inside the base host's chrome, which already shows the logo (so the card does not repeat it). */
export const InBaseChrome = createContext(false);

/** The centred card the sign-in, GitHub callback, bootstrap and reset pages share. */
export function AuthLayout({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const inChrome = useContext(InBaseChrome);
  return (
    <div className="flex min-h-[70vh] items-start justify-center bg-background px-6 pt-[8vh] pb-6">
      <div className="flex w-full max-w-md flex-col gap-6">
        <div className="flex flex-col gap-1.5">
          {!inChrome && (
            <div className="mb-2 flex items-center gap-2">
              <div className="flex size-[22px] items-center justify-center rounded-md bg-primary">
                <NetworkIcon className="size-3.5 text-primary-foreground" aria-hidden />
              </div>
              <span className="font-semibold tracking-tight">WhyGraph</span>
            </div>
          )}
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {description && <p className="text-sm text-muted-foreground">{description}</p>}
        </div>
        <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card">{children}</div>
        {footer && <div className="text-sm text-muted-foreground">{footer}</div>}
      </div>
    </div>
  );
}
