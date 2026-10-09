import { Fragment, type ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { MenuIcon } from "lucide-react";
import { membersApi, portalKey } from "../../api";
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
import { buildCrumbs } from "./crumbs";
import { BudgetBanner } from "./BudgetBanner";
import { ReaderBanner } from "./ReaderBanner";
import { LiveRegion } from "./LiveRegion";

/**
 * The page header: breadcrumbs on the left, an optional actions slot on the right
 * (e.g. "Scan now"). The shell renders it with route-derived crumbs; a page that
 * has actions renders its own inside its content.
 */
export function PageHeader({
  projectName,
  actions,
}: {
  projectName?: string;
  actions?: ReactNode;
}) {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const setNavOpen = useUi((s) => s.setNavOpen);
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
  const crumbs = buildCrumbs(pathname, projectName, memberName);
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
                  {last || !crumb.to ? (
                    <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
                  ) : (
                    <BreadcrumbLink
                      render={<Link to={crumb.to.to} params={crumb.to.params} />}
                    >
                      {crumb.label}
                    </BreadcrumbLink>
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
          <Sidebar slug={slug} projectName={projectName} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <ReaderBanner />
        <BudgetBanner />
        <PageHeader projectName={projectName} />
        <main className="flex min-h-0 flex-1 flex-col overflow-auto">{children}</main>
      </div>
      <LiveRegion />
    </div>
  );
}
