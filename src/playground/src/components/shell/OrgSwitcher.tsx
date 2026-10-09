import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckIcon, ChevronsUpDownIcon } from "lucide-react";
import { accountApi, type OrgEntry, type PortalState } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { isProduction, signedInAs } from "../../lib/identity";
import { orgRoleLabel } from "../../lib/labels";
import { hardNavigate } from "../../lib/navigation";
import { Badge } from "../ui/badge";
import { Skeleton } from "../ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";

/** The query key of the caller's organizations (`GET /api/account/orgs`), shared with the org picker. */
export const ACCOUNT_ORGS_KEY = ["@account", "orgs"] as const;

/** An org role in words; an instance admin outside the org reads it. */
export function orgRoleText(role: string | null | undefined): string {
  if (!role) return "";
  return role === "reader" ? "Read-only" : orgRoleLabel(role);
}

/** The "New" marker of an org the caller has not opened yet (the welcome flag, §4.12). */
function NewDot() {
  return (
    <span className="flex items-center gap-1 text-[11px] text-primary-text" data-testid="org-new">
      <span aria-hidden className="size-1.5 rounded-full bg-primary-text" />
      New
    </span>
  );
}

/**
 * The org switcher at the top of the sidebar (NAV-2). Production, org host: a menu
 * with who is signed in, the current org (role badge), the caller's other orgs
 * (fetched on first open, cached 5 minutes; each a hard navigation to its host),
 * then "All organizations" and "Create organization" on the base host. Local mode:
 * a static "Local" label.
 */
export function OrgSwitcher({ state }: { state: PortalState | undefined }) {
  if (!isProduction(state)) {
    return (
      <span className="ml-auto rounded border border-border px-1.5 py-px text-[11px] text-muted-foreground" data-testid="org-label">
        Local
      </span>
    );
  }
  return <OrgMenu state={state} />;
}

function OrgMenu({ state }: { state: PortalState | undefined }) {
  const [opened, setOpened] = useState(false);
  const orgs = useQuery({
    queryKey: ACCOUNT_ORGS_KEY,
    queryFn: accountApi.orgs,
    staleTime: 5 * 60_000,
    enabled: opened,
  });
  const org = state?.org;
  const base = state?.base_url?.replace(/\/$/, "") ?? "";
  const from = org ? `&from=${encodeURIComponent(org.slug)}` : "";
  const others: OrgEntry[] = (orgs.data ?? []).filter((o) => o.slug !== org?.slug);
  const who = signedInAs(state?.user);
  return (
    <DropdownMenu onOpenChange={(open) => open && setOpened(true)}>
      <DropdownMenuTrigger
        aria-label={`Organization: ${org?.name ?? "none"}. Switch organization`}
        data-testid="org-switcher"
        className="ml-auto flex h-6 min-w-0 max-w-[9rem] items-center gap-1 rounded-md border border-border px-1.5 text-[12px] font-medium outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="truncate">{org?.name ?? "No organization"}</span>
        <ChevronsUpDownIcon className="size-3 shrink-0 text-muted-foreground" aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        {who && (
          <DropdownMenuGroup>
            <DropdownMenuLabel className="truncate">Signed in as {who}</DropdownMenuLabel>
          </DropdownMenuGroup>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          {org && (
            <DropdownMenuItem aria-current="true" className="bg-primary-soft text-primary-text" data-testid="org-current">
              <CheckIcon className="size-3.5" aria-hidden />
              <span className="min-w-0 flex-1 truncate font-medium">{org.name}</span>
              <Badge variant="outline">{orgRoleText(org.role)}</Badge>
            </DropdownMenuItem>
          )}
          {orgs.isLoading && <Skeleton className="mx-1.5 my-1 h-5" />}
          {orgs.isError && (
            <div className="px-1.5 py-1 text-xs text-muted-foreground">{errorMessage(orgs.error)}</div>
          )}
          {others.map((o) => (
            <DropdownMenuItem key={o.slug} onClick={() => void hardNavigate(o.url)} data-testid={`org-${o.slug}`}>
              <span className="size-3.5 shrink-0" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{o.name}</span>
              {o.new && <NewDot />}
              <span className="text-xs text-muted-foreground">{orgRoleText(o.role)}</span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => void hardNavigate(`${base}/orgs?stay=1${from}`)}>
          All organizations
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => void hardNavigate(`${base}/orgs/new`)}>Create organization</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
