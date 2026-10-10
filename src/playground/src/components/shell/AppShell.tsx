import { Fragment, useSyncExternalStore, type MouseEvent, type ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MenuIcon } from "lucide-react";
import { membersApi, portalKey, type ChatSession, type ScanRun } from "../../api";
import { chatSessionsKey } from "../../lib/chatSessions";
import { usePortalState } from "../../lib/identity";
import { runTitle } from "../../lib/scanFormat";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { useUi } from "../../store";
import { Button } from "../ui/button";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "../ui/breadcrumb";
import { Sheet, SheetContent, SheetTitle } from "../ui/sheet";
import { Sidebar } from "./Sidebar";
import { buildCrumbs, pageTitle, scanRunKey } from "./crumbs";
import { BannerStack } from "./BannerStack";
import { LiveRegion } from "./LiveRegion";

/**
 * A query's cached data, without creating a cache entry or fetching (`null` key:
 * nothing). The header only names what a page has already loaded.
 */
function useCachedData<T>(key: readonly unknown[] | null): T | undefined {
  const queryClient = useQueryClient();
  return useSyncExternalStore(
    (onChange) => queryClient.getQueryCache().subscribe(onChange),
    () => (key ? queryClient.getQueryData<T>(key) : undefined),
  );
}

/**
 * The page header: breadcrumbs on the left, an optional actions slot on the right
 * (e.g. "Scan now"). The shell renders it with route-derived crumbs; a page that
 * has actions renders its own inside its content. It also sets the document title
 * ("<page> · <project> · WhyGraph").
 */
export function PageHeader({
  projectName,
  actions,
}: {
  projectName?: string;
  actions?: ReactNode;
}) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const search = useRouterState({ select: (s) => s.location.search }) as { step?: unknown };
  const setNavOpen = useUi((s) => s.setNavOpen);
  const state = usePortalState().data;
  // A member drill-down names the member (the page loads the same list).
  const memberUid = /^\/usage\/members\/([^/]+)/.exec(pathname)?.[1];
  const members = useQuery({
    queryKey: portalKey("members"),
    queryFn: membersApi.list,
    enabled: !!memberUid,
  });
  const memberName = memberUid
    ? members.data?.find((m) => m.uid === decodeURIComponent(memberUid))?.display_name
    : undefined;
  // Titles the path does not carry, read from the query cache only (the Chats
  // section and the run page fetch them; a viewer must not fire a sessions call).
  const chat = /^\/p\/([^/]+)\/chat\/(\d+)$/.exec(pathname);
  const sessions = useCachedData<ChatSession[]>(chat ? chatSessionsKey(decodeURIComponent(chat[1])) : null);
  const chatTitle = chat ? sessions?.find((s) => s.id === Number(chat[2]))?.title : undefined;
  const runMatch = /^\/p\/([^/]+)\/scans\/(\d+)$/.exec(pathname);
  const run = useCachedData<ScanRun>(runMatch ? scanRunKey(decodeURIComponent(runMatch[1]), Number(runMatch[2])) : null);
  const runCrumb = run ? runTitle(run) : undefined;
  const wizardStep = search.step === "setup" || search.step === "configure" ? search.step : undefined;
  const crumbs = buildCrumbs(pathname, {
    projectName,
    memberName,
    chatTitle,
    runTitle: runCrumb,
    wizardStep,
    orgUsage: !!state?.usage?.org,
  });
  const inProject = pathname.startsWith("/p/");
  useDocumentTitle(pageTitle(crumbs, pathname), inProject ? (projectName ?? crumbs[1]?.label) : undefined);
  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
      <Button
        variant="ghost"
        size="icon-sm"
        className="md:hidden"
        aria-label="Open navigation"
        onClick={() => setNavOpen(true)}
      >
        <MenuIcon />
      </Button>
      <Breadcrumb className="min-w-0 flex-1">
        <BreadcrumbList>
          {crumbs.map((crumb, i) => {
            const last = i === crumbs.length - 1;
            return (
              <Fragment key={`${i}:${crumb.label}`}>
                <BreadcrumbItem>
                  {last ? (
                    <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
                  ) : crumb.to ? (
                    <BreadcrumbLink
                      render={<Link to={crumb.to.to} params={crumb.to.params} />}
                    >
                      {crumb.label}
                    </BreadcrumbLink>
                  ) : (
                    <span>{crumb.label}</span>
                  )}
                </BreadcrumbItem>
                {!last && <BreadcrumbSeparator />}
              </Fragment>
            );
          })}
        </BreadcrumbList>
      </Breadcrumb>
      {actions}
    </header>
  );
}

/** Move focus to the main region (the skip link's target) without a hash navigation. */
function skipToMain(event: MouseEvent<HTMLAnchorElement>) {
  event.preventDefault();
  document.getElementById("main")?.focus();
}

/**
 * Sidebar + page header + content, for both scopes. `slug` present means project
 * scope. Below 768 px the sidebar becomes a sheet (opened from the header).
 */
export function AppShell({
  slug,
  projectName,
  children,
}: {
  slug?: string;
  projectName?: string;
  children: ReactNode;
}) {
  const navOpen = useUi((s) => s.navOpen);
  const setNavOpen = useUi((s) => s.setNavOpen);
  return (
    <div className="flex h-screen bg-background text-foreground">
      {/* The first focusable element (NAV-3): straight past the sidebar to the page. */}
      <a
        href="#main"
        onClick={skipToMain}
        className="sr-only z-50 rounded-md bg-background px-3 py-2 text-sm font-medium shadow-md outline-none focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus-visible:ring-2 focus-visible:ring-ring"
      >
        Skip to content
      </a>
      <div className="hidden md:block">
        <Sidebar slug={slug} projectName={projectName} />
      </div>
      <Sheet open={navOpen} onOpenChange={setNavOpen}>
        <SheetContent
          side="left"
          showCloseButton
          className="w-60 gap-0 p-0 data-[side=left]:w-60 data-[side=left]:sm:max-w-60 sm:max-w-60"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          {/* `inSheet` keeps the header row clear of the sheet's own close button. */}
          <Sidebar slug={slug} projectName={projectName} inSheet />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <BannerStack slug={slug} />
        <PageHeader projectName={projectName} />
        <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col overflow-auto outline-none">
          {children}
        </main>
      </div>
      <LiveRegion />
    </div>
  );
}
