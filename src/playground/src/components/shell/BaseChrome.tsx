import type { ReactNode } from "react";
import { Link, useNavigate, useRouterState, useSearch } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeftIcon, ChevronDownIcon, NetworkIcon } from "lucide-react";
import { accountApi } from "../../api";
import { usePortalState, useSignOut } from "../../lib/identity";
import { useDocumentTitle } from "../../lib/useDocumentTitle";
import { InBaseChrome } from "../auth/AuthLayout";
import { UserAvatar } from "../auth/UserAvatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import { LiveRegion } from "./LiveRegion";
import { ACCOUNT_ORGS_KEY } from "./OrgSwitcher";

// The page names of the base host, for the document title.
const BASE_TITLES: Record<string, string> = {
  "/signin": "Sign in",
  "/auth/github": "Signing in",
  "/auth/github-app": "GitHub",
  "/reset": "Reset password",
  "/orgs": "Organizations",
  "/orgs/new": "Create organization",
  "/admin": "Administration",
  "/account": "Account",
  "/connect": "Connect",
};

const NAV_LINK =
  "rounded-md px-2 py-1 text-muted-foreground outline-none hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring data-[status=active]:font-medium data-[status=active]:text-foreground";

/**
 * The base host's chrome (NAV-7, MEM-6): the logo, a nav (Organizations, and
 * Administration for instance admins), an avatar menu (Account, Sign out) and,
 * when the address carries `?from=<slug>` naming one of the caller's orgs, a
 * "Back to <org>" link. Signed out, the page renders alone. `title` overrides the
 * document title (an unknown address).
 */
export function BaseChrome({ children, title }: { children: ReactNode; title?: string }) {
  const state = usePortalState().data;
  const user = state?.user;
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const { from } = useSearch({ strict: false }) as { from?: string };
  const navigate = useNavigate();
  const signOut = useSignOut();
  // `from` is only a hint: it names an org only if the caller is one of its members.
  const orgs = useQuery({
    queryKey: ACCOUNT_ORGS_KEY,
    queryFn: accountApi.orgs,
    staleTime: 5 * 60_000,
    enabled: !!user && !!from,
  });
  const back = from ? orgs.data?.find((o) => o.slug === from) : undefined;
  useDocumentTitle(title ?? BASE_TITLES[pathname.replace(/\/+$/, "") || "/"]);

  const main = (
    <main id="main" tabIndex={-1} className="outline-none">
      {children}
    </main>
  );
  if (!user) {
    return (
      <>
        {main}
        <LiveRegion />
      </>
    );
  }
  return (
    <InBaseChrome.Provider value>
      <header className="border-b border-border">
        <nav aria-label="Account" className="flex items-center gap-1 px-4 py-2 text-sm sm:gap-2 sm:px-6">
          <Link
            to="/orgs"
            search={{ stay: true, from }}
            aria-label="WhyGraph"
            className="mr-1 flex items-center gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring sm:mr-3"
          >
            <span className="flex size-[22px] items-center justify-center rounded-md bg-primary">
              <NetworkIcon className="size-3.5 text-primary-foreground" aria-hidden />
            </span>
            <span className="hidden font-semibold tracking-tight sm:inline">WhyGraph</span>
          </Link>
          <Link to="/orgs" search={{ stay: true, from }} className={NAV_LINK}>
            Organizations
          </Link>
          {user.is_instance_admin && (
            <Link to="/admin" className={NAV_LINK}>
              Administration
            </Link>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label="Account menu"
              className="ml-auto flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1 outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
            >
              <UserAvatar name={user.display_name} url={user.avatar_url} className="size-6" />
              <span className="hidden max-w-[12rem] truncate sm:inline">{user.display_name}</span>
              <ChevronDownIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              <DropdownMenuItem onClick={() => void navigate({ to: "/account", search: { from } })}>
                Account
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => void signOut()}>Sign out</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </nav>
        {back && (
          <div className="px-4 pb-2 text-sm sm:px-6">
            <a
              href={back.url}
              data-testid="back-to-org"
              className="inline-flex max-w-full items-center gap-1.5 rounded-md text-primary-text outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ArrowLeftIcon className="size-3.5 shrink-0" aria-hidden />
              <span className="truncate">Back to {back.name}</span>
            </a>
          </div>
        )}
      </header>
      {main}
      <LiveRegion />
    </InBaseChrome.Provider>
  );
}
