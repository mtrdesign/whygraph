import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { adminApi, type AdminOrg, type AdminProjectRow, type AdminUser } from "../../api";
import { useUi } from "../../store";
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "../ui/command";
import { Badge } from "../ui/badge";
import { ADMIN_NAV } from "./adminNav";

// Cmd-K inside the admin app (plan 59, §4.14). It runs its own queries for the
// lists it searches, while it is open, rather than relying on a list page having
// cached them. A result opens its detail page, a deleted one marked with a "Deleted"
// badge.

const MAX = 8;

const userLabel = (u: AdminUser) => (u.github_login ? `@${u.github_login}` : (u.email ?? u.uid));

/** Users on name, login and email; Organizations on name and slug (the fields their lists search). */
export function userMatches(u: AdminUser, term: string): boolean {
  return [u.display_name, u.github_login, u.email].some((f) => !!f && f.toLowerCase().includes(term));
}
export function orgMatches(o: AdminOrg, term: string): boolean {
  return [o.name, o.slug].some((f) => f.toLowerCase().includes(term));
}

/** Projects on name, slug and org slug. */
export function projectMatches(p: AdminProjectRow, term: string): boolean {
  return [p.name, p.slug, p.org].some((f) => f.toLowerCase().includes(term));
}

/** A `CommandDialog` over the admin pages, the users and the organizations. */
export function AdminCommandPalette() {
  const open = useUi((s) => s.paletteOpen);
  const setOpen = useUi((s) => s.setPaletteOpen);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const term = query.trim().toLowerCase();
  const users = useQuery({ queryKey: ["@admin", "users"], queryFn: adminApi.users, enabled: open });
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs, enabled: open });
  const projects = useQuery({ queryKey: ["@admin", "projects"], queryFn: adminApi.projects, enabled: open });

  const close = () => {
    setOpen(false);
    setQuery("");
  };
  const go = (fn: () => void) => () => {
    fn();
    close();
  };

  const pages = [
    ...ADMIN_NAV.map((i) => ({ label: i.label, to: i.to })),
    { label: "Account", to: "/admin/account" },
  ].filter((p) => term === "" || p.label.toLowerCase().includes(term));
  const shownUsers = term === "" ? [] : (users.data ?? []).filter((u) => userMatches(u, term)).slice(0, MAX);
  const shownOrgs = term === "" ? [] : (orgs.data ?? []).filter((o) => orgMatches(o, term)).slice(0, MAX);
  const shownProjects =
    term === "" ? [] : (projects.data?.projects ?? []).filter((p) => projectMatches(p, term)).slice(0, MAX);
  const loading = term !== "" && (users.isLoading || orgs.isLoading || projects.isLoading);

  return (
    <CommandDialog
      open={open}
      onOpenChange={(next) => (next ? setOpen(true) : close())}
      title="Command menu"
      description="Go to an admin page, a user, an organization or a project."
    >
      <Command shouldFilter={false}>
        <CommandInput
          autoFocus
          value={query}
          onValueChange={setQuery}
          placeholder="Go to a page, user, organization or project..."
        />
        <CommandList>
          {!loading && <CommandEmpty>No results.</CommandEmpty>}
          {pages.length > 0 && (
            <CommandGroup heading="Go to">
              {pages.map((p) => (
                <CommandItem key={p.to} value={`page:${p.to}`} onSelect={go(() => void navigate({ to: p.to }))}>
                  {p.label}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {term !== "" && (users.isLoading || shownUsers.length > 0) && (
            <CommandGroup heading="Users">
              {users.isLoading && <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>}
              {shownUsers.map((u) => (
                <CommandItem
                  key={u.uid}
                  value={`user:${u.uid}`}
                  onSelect={go(() => void navigate({ to: "/admin/users/$uid", params: { uid: u.uid } }))}
                  className="gap-2"
                >
                  <span className="font-medium">{u.display_name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{userLabel(u)}</span>
                  {u.is_instance_admin && <span className="text-xs text-muted-foreground">admin</span>}
                  {u.disabled && <span className="text-xs text-muted-foreground">disabled</span>}
                  {u.deleted_at && <Badge variant="destructive">Deleted</Badge>}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {term !== "" && (orgs.isLoading || shownOrgs.length > 0) && (
            <CommandGroup heading="Organizations">
              {orgs.isLoading && <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>}
              {shownOrgs.map((o) => (
                <CommandItem
                  key={o.slug}
                  value={`org:${o.slug}`}
                  onSelect={go(() => void navigate({ to: "/admin/orgs/$slug", params: { slug: o.slug } }))}
                  className="gap-2"
                >
                  <span className="font-medium">{o.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">{o.slug}</span>
                  {o.deleted_at && <Badge variant="destructive">Deleted</Badge>}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {term !== "" && (projects.isLoading || shownProjects.length > 0) && (
            <CommandGroup heading="Projects">
              {projects.isLoading && <div className="px-3 py-2 text-xs text-muted-foreground">Loading...</div>}
              {shownProjects.map((p) => (
                <CommandItem
                  key={`${p.org}/${p.slug}`}
                  value={`project:${p.org}/${p.slug}`}
                  onSelect={go(
                    () => void navigate({ to: "/admin/projects/$org/$slug", params: { org: p.org, slug: p.slug } }),
                  )}
                  className="gap-2"
                >
                  <span className="font-medium">{p.name}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">
                    {p.org}/{p.slug}
                  </span>
                  {p.deleted_at && <Badge variant="destructive">Deleted</Badge>}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  );
}
