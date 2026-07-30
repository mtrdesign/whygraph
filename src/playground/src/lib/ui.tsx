import { forwardRef } from "react";
import { clsx } from "clsx";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

// A small, self-contained set of Tailwind-styled primitives in the shadcn/Linear
// idiom — enough for the panel without pulling the full shadcn generator.

const KIND_COLORS: Record<string, string> = {
  file: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  directory: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  class: "bg-violet-500/15 text-violet-300 border-violet-500/30",
  method: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  function: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  variable: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  import: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};

export function KindBadge({ kind }: { kind: string }) {
  const cls = KIND_COLORS[kind] ?? "bg-slate-500/15 text-slate-300 border-slate-500/30";
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        cls,
      )}
    >
      {kind}
    </span>
  );
}

export function CoverageDot({ analyzed }: { analyzed: boolean }) {
  return (
    <span
      title={analyzed ? "Rationale generated" : "Not yet analyzed"}
      className={clsx(
        "inline-block h-2 w-2 shrink-0 rounded-full",
        analyzed ? "bg-emerald-400" : "bg-slate-600",
      )}
    />
  );
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost";
  children: ReactNode;
}

export function Button({ variant = "primary", className, children, ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      className={clsx(
        "inline-flex items-center justify-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" &&
          "bg-accent text-white hover:bg-accent2 disabled:hover:bg-accent",
        variant === "ghost" && "text-muted hover:bg-panel2 hover:text-fg",
        className,
      )}
    >
      {children}
    </button>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-border border-t-accent2" />
      {label}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="p-4 text-sm text-muted">{children}</div>;
}

// ---- form primitives (added for the Chat view) -----------------------------
//
// Same flat-token vocabulary as the rest of this file: bg-panel2 surfaces,
// border-border edges, accent2 focus rings. No Radix, no CVA.

const FIELD_CLASS =
  "w-full rounded-md border border-border bg-panel2 px-3 py-2 text-sm text-fg " +
  "placeholder:text-muted focus:border-accent2 focus:outline-none " +
  "disabled:cursor-not-allowed disabled:opacity-50";

// forwardRef so the Composer can refocus after sending.
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return (
      <textarea
        {...rest}
        ref={ref}
        className={clsx(FIELD_CLASS, "resize-none", className)}
      />
    );
  },
);

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...rest} className={clsx(FIELD_CLASS, className)} />;
}

export function Select({
  className,
  children,
  ...rest
}: SelectHTMLAttributes<HTMLSelectElement>) {
  // A native <select> on purpose: it is keyboard- and screen-reader-correct for
  // free, and a custom listbox would be the single largest chunk of new UI code
  // here for no functional gain.
  return (
    <select {...rest} className={clsx(FIELD_CLASS, "cursor-pointer", className)}>
      {children}
    </select>
  );
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  children: ReactNode;
}

export function IconButton({ label, className, children, ...rest }: IconButtonProps) {
  return (
    <button
      {...rest}
      title={label}
      aria-label={label}
      className={clsx(
        "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted " +
          "transition-colors hover:bg-panel2 hover:text-fg disabled:opacity-40",
        className,
      )}
    >
      {children}
    </button>
  );
}
