import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useBlocker } from "@tanstack/react-router";
import { cn } from "../../lib/utils";
import { PageContainer } from "../layout/PageContainer";
import { ConfirmDialog } from "../portal/ConfirmDialog";

/** One entry of the section list: `id` is the `?section=` value and the anchor `settings-<id>`. */
export interface SettingsNavItem {
  id: string;
  label: string;
}

interface LayoutContext {
  /** A section mounted (so the observer and the deep link can find it). */
  mounted: (id: string) => void;
  /** A section form's unsaved state, for the leave guard. */
  setDirty: (key: string, label: string | null) => void;
}

const Ctx = createContext<LayoutContext | null>(null);

/** `settings-<id>`: the element a section renders, the nav's scroll target. */
export const sectionAnchor = (id: string) => `settings-${id}`;

function scrollToSection(id: string, smooth: boolean) {
  const el = document.getElementById(sectionAnchor(id));
  // jsdom has no scrollIntoView; a browser always has.
  if (el && typeof el.scrollIntoView === "function") {
    el.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" });
  }
}

/**
 * Register a section form's unsaved changes with the page's leave guard; a
 * no-op outside a `SettingsLayout`.
 */
export function useSettingsDirty(key: string, label: string, dirty: boolean) {
  const ctx = useContext(Ctx);
  const setDirty = ctx?.setDirty;
  useEffect(() => {
    if (!setDirty) return;
    setDirty(key, dirty ? label : null);
    return () => setDirty(key, null);
  }, [setDirty, key, label, dirty]);
}

/**
 * The settings page frame (SET-1, plan section 0.3 #22) for project, org and
 * local global settings: a heading, then a sticky section list beside the
 * sections (a sticky, sideways-scrolling strip above them below `md`). The
 * active entry follows the scroll position (IntersectionObserver); `initial`
 * (the page's `?section=`) is scrolled into view once its section mounts.
 * Leaving the page with an unsaved section form asks first (router blocker and
 * `beforeunload`); changing only the query string never does.
 */
export function SettingsLayout({
  title,
  description,
  sections,
  initial,
  notices,
  children,
}: {
  title: string;
  description?: ReactNode;
  sections: SettingsNavItem[];
  initial?: string;
  /** Read-only and edge-state notices, above the sections. */
  notices?: ReactNode;
  children: ReactNode;
}) {
  const ids = useMemo(() => sections.map((s) => s.id), [sections]);
  const key = ids.join(",");
  const [active, setActive] = useState<string | undefined>(initial && ids.includes(initial) ? initial : ids[0]);
  const [version, setVersion] = useState(0);
  const deepLinked = useRef(false);
  const pausedUntil = useRef(0);
  const [dirty, setDirtyMap] = useState<Record<string, string>>({});

  const mounted = useCallback(
    (id: string) => {
      setVersion((v) => v + 1);
      if (id === initial && !deepLinked.current) {
        deepLinked.current = true;
        pausedUntil.current = Date.now() + 1000;
        // Once now, and again after the sections above have had a moment to load.
        requestAnimationFrame(() => scrollToSection(id, false));
        window.setTimeout(() => scrollToSection(id, false), 400);
      }
    },
    [initial],
  );
  const setDirty = useCallback((k: string, label: string | null) => {
    setDirtyMap((prev) => {
      if (label === null) {
        if (!(k in prev)) return prev;
        const next = { ...prev };
        delete next[k];
        return next;
      }
      return prev[k] === label ? prev : { ...prev, [k]: label };
    });
  }, []);

  // A later `?section=` on the same page (a link to another section) scrolls too.
  const lastInitial = useRef(initial);
  useEffect(() => {
    if (initial === lastInitial.current) return;
    lastInitial.current = initial;
    if (initial && document.getElementById(sectionAnchor(initial))) {
      setActive(initial);
      pausedUntil.current = Date.now() + 800;
      scrollToSection(initial, true);
    }
  }, [initial]);

  // The active entry: the first section whose top part is on screen.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const visible = new Map<string, boolean>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) visible.set(e.target.id.replace(/^settings-/, ""), e.isIntersecting);
        if (Date.now() < pausedUntil.current) return;
        const first = ids.find((id) => visible.get(id));
        if (first) setActive(first);
      },
      { rootMargin: "0px 0px -65% 0px", threshold: 0 },
    );
    for (const id of ids) {
      const el = document.getElementById(sectionAnchor(id));
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
    // `key` stands for `ids`; `version` re-observes sections that mounted late.
  }, [key, version]);

  const unsaved = Object.values(dirty);
  const blocker = useBlocker({
    shouldBlockFn: ({ current, next }) => unsaved.length > 0 && current.pathname !== next.pathname,
    enableBeforeUnload: () => unsaved.length > 0,
    withResolver: true,
  });

  const ctx = useMemo(() => ({ mounted, setDirty }), [mounted, setDirty]);
  const go = (id: string) => {
    setActive(id);
    pausedUntil.current = Date.now() + 800;
    scrollToSection(id, true);
  };

  return (
    <Ctx.Provider value={ctx}>
      <PageContainer width="default">
        <div className="mb-5 flex flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">{title}</h1>
          {description && <div className="text-[13px] text-muted-foreground">{description}</div>}
        </div>
        <div className="flex flex-col gap-5 md:flex-row md:gap-8">
          <nav
            aria-label="Settings sections"
            className="sticky top-0 z-10 -mx-4 border-b border-border bg-background px-4 py-1.5 sm:-mx-6 sm:px-6 md:top-5 md:mx-0 md:h-fit md:w-44 md:shrink-0 md:border-0 md:p-0"
          >
            <ul data-scroll-x className="flex gap-1 overflow-x-auto md:flex-col md:gap-0.5 md:overflow-visible">
              {sections.map((s) => (
                <li key={s.id} className="shrink-0">
                  <button
                    type="button"
                    aria-current={active === s.id ? "true" : undefined}
                    onClick={() => go(s.id)}
                    className={cn(
                      "w-full rounded-md px-2 py-1.5 text-left text-sm whitespace-nowrap text-muted-foreground hover:bg-muted hover:text-foreground",
                      active === s.id && "bg-primary-soft font-medium text-primary-text",
                    )}
                  >
                    {s.label}
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <div className="flex min-w-0 flex-1 flex-col gap-5">
            {notices}
            {children}
          </div>
        </div>
      </PageContainer>
      <ConfirmDialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.();
        }}
        title="Leave without saving?"
        description={`Your changes to ${listText(unsaved)} are not saved.`}
        confirmLabel="Leave"
        pending={false}
        error={null}
        onConfirm={() => blocker.proceed?.()}
      />
    </Ctx.Provider>
  );
}

function listText(items: string[]): string {
  const unique = [...new Set(items)];
  if (unique.length <= 1) return unique[0] ?? "this page";
  return `${unique.slice(0, -1).join(", ")} and ${unique[unique.length - 1]}`;
}

/**
 * One settings section: a card with a heading, anchored `settings-<id>` for the
 * section list. `danger` tints it for the Danger zone.
 */
export function SettingsSection({
  id,
  title,
  description,
  danger = false,
  testId,
  children,
}: {
  id: string;
  title: string;
  description?: ReactNode;
  danger?: boolean;
  testId?: string;
  children: ReactNode;
}) {
  const ctx = useContext(Ctx);
  const mounted = ctx?.mounted;
  useEffect(() => {
    mounted?.(id);
  }, [mounted, id]);
  return (
    <section
      id={sectionAnchor(id)}
      aria-label={title}
      data-testid={testId}
      className={cn(
        "flex min-w-0 scroll-mt-14 flex-col gap-4 rounded-xl border bg-card p-4 shadow-card sm:p-5 md:scroll-mt-5",
        danger ? "border-destructive/40" : "border-border",
      )}
    >
      <div>
        <h2 className={cn("text-sm font-semibold", danger && "text-destructive")}>{title}</h2>
        {description && <div className="mt-0.5 text-xs text-muted-foreground">{description}</div>}
      </div>
      {children}
    </section>
  );
}
