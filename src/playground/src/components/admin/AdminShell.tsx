import { Fragment, type MouseEvent, type ReactNode } from "react";
import { Link, useRouterState } from "@tanstack/react-router";
import { MenuIcon } from "lucide-react";
import { cn } from "cn";
import { useQuery } from "@tanstack/react-query";
import { adminApi, type AdminMe } from "../../api";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { useUi } from "../../store";
import { AnnouncementBanner } from "../shell/AnnouncementBanner";
import { LiveRegion } from "../shell/LiveRegion";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "../ui/breadcrumb";
import { Button } from "../ui/button";
import { Sheet, SheetContent, SheetTitle } from "../ui/sheet";
import { adminCrumbs } from "./adminNav";
import { AdminSidebar } from "./AdminSidebar";

/**
 * The admin header: crumbs (the middle ones collapse on a phone), the menu button
 * below `md` and the document title. It is not `PageHeader`, which builds org
 * crumbs and would title every admin page "Not found".
 */
function AdminHeader() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const setNavOpen = useUi((s) => s.setNavOpen);
  // A detail page names itself once its query has loaded (this only reads the cache).
  const [, , module, rawKey, rawSlug] = pathname.split("/");
  const key = rawKey ? decodeURIComponent(rawKey) : "";
  const org = useQuery({
    queryKey: ["@admin", "org", key],
    queryFn: () => adminApi.org(key),
    enabled: false,
  }).data?.name;
  const user = useQuery({
    queryKey: ["@admin", "user", key],
    queryFn: () => adminApi.user(key),
    enabled: false,
  }).data?.display_name;
  const projectSlug = rawSlug ? decodeURIComponent(rawSlug) : "";
  const project = useQuery({
    queryKey: ["@admin", "project", key, projectSlug],
    queryFn: () => adminApi.project(key, projectSlug),
    enabled: false,
  }).data?.name;
  const { heading, crumbs } = adminCrumbs(pathname, {
    project: module === "projects" ? project : undefined,
    org: module === "orgs" ? org : undefined,
    user: module === "users" ? user : undefined,
  });
  useDocumentTitle(heading, "Administration");
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
        <BreadcrumbList className="flex-nowrap overflow-hidden" data-testid="breadcrumbs">
          {crumbs.map((crumb, i) => {
            const last = i === crumbs.length - 1;
            const middle = i > 0 && !last;
            return (
              <Fragment key={`${i}:${crumb.label}`}>
                {i === 1 && !last && (
                  <>
                    <BreadcrumbItem className="min-w-0 shrink-0 sm:hidden" data-testid="crumbs-collapsed">
                      <span aria-label="Earlier pages hidden">...</span>
                    </BreadcrumbItem>
                    <BreadcrumbSeparator className="shrink-0 sm:hidden" />
                  </>
                )}
                <BreadcrumbItem
                  className={cn(
                    last ? "min-w-0 max-w-[70%] shrink-0" : "min-w-0 shrink",
                    i === 0 && !last && "max-w-[40%]",
                    middle && "max-sm:hidden",
                  )}
                  title={crumb.label}
                >
                  {last ? (
                    <BreadcrumbPage className="truncate">{crumb.label}</BreadcrumbPage>
                  ) : crumb.to ? (
                    <BreadcrumbLink className="truncate" render={<Link to={crumb.to} />}>
                      {crumb.label}
                    </BreadcrumbLink>
                  ) : (
                    <span className="truncate">{crumb.label}</span>
                  )}
                </BreadcrumbItem>
                {!last && <BreadcrumbSeparator className={cn("shrink-0", middle && "max-sm:hidden")} />}
              </Fragment>
            );
          })}
        </BreadcrumbList>
      </Breadcrumb>
    </header>
  );
}

/** Move focus to the main region (the skip link's target) without a hash navigation. */
function skipToMain(event: MouseEvent<HTMLAnchorElement>) {
  event.preventDefault();
  document.getElementById("main")?.focus();
}

/**
 * Sidebar + header + content for the admin app, mirroring `AppShell`: a skip
 * link, the desktop sidebar, the phone sheet, `<main id="main">` and the live
 * region `RouteFocus` needs. It mounts no `BannerStack` (org banners), only the platform announcement.
 */
export function AdminShell({ me, children }: { me: AdminMe; children: ReactNode }) {
  const navOpen = useUi((s) => s.navOpen);
  const setNavOpen = useUi((s) => s.setNavOpen);
  return (
    <div className="flex h-screen bg-background text-foreground">
      <a
        href="#main"
        onClick={skipToMain}
        className="sr-only z-50 rounded-md bg-background px-3 py-2 text-sm font-medium shadow-md outline-none focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus-visible:ring-2 focus-visible:ring-ring"
      >
        Skip to content
      </a>
      <div className="hidden md:block">
        <AdminSidebar me={me} />
      </div>
      <Sheet open={navOpen} onOpenChange={setNavOpen}>
        <SheetContent
          side="left"
          showCloseButton
          className="w-60 gap-0 p-0 data-[side=left]:w-60 data-[side=left]:sm:max-w-60 sm:max-w-60"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <AdminSidebar me={me} inSheet />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <AnnouncementBanner />
        <AdminHeader />
        <main id="main" tabIndex={-1} className="flex min-h-0 flex-1 flex-col overflow-auto outline-none">
          {children}
        </main>
      </div>
      <LiveRegion />
    </div>
  );
}
