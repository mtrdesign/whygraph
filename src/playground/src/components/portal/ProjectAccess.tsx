import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { accessApi, portalKey, projectKey, type AccessPerson, type ProjectRole } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { formatUsd } from "../../lib/format";
import { UserAvatar } from "../auth/UserAvatar";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { Switch } from "../ui/switch";
import { nativeSelectClass } from "./Field";

const ROLES: ProjectRole[] = ["viewer", "contributor", "admin"];

const DEFAULT_LABEL: Record<string, string> = {
  admin: "Admin",
  contributor: "Contributor",
  viewer: "Viewer",
  none: "no access",
};

const personName = (p: AccessPerson) => p.name || (p.login ? `@${p.login}` : p.uid);

/**
 * A project's access (production, `project.access`): the Restricted switch, who holds which
 * role and why (org admin, a grant on this project, or the organization's default), and
 * adding someone from the org's members.
 */
export function ProjectAccess({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const key = projectKey(slug, "access");
  const api = accessApi(slug);
  const data = useQuery({ queryKey: key, queryFn: api.get });
  const [addUid, setAddUid] = useState("");
  const [addRole, setAddRole] = useState<ProjectRole>("viewer");

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: key });
    await queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
    await queryClient.invalidateQueries({ queryKey: portalKey("projects") });
  };
  const restrict = useMutation({
    mutationFn: (value: boolean) => api.setRestricted(value),
    onSuccess: refresh,
    onError: (err) => toast.error(authMessage(err)),
  });
  const setGrant = useMutation({
    mutationFn: ({ uid, role }: { uid: string; role: ProjectRole }) => api.setGrant(uid, role),
    onSuccess: async () => {
      setAddUid("");
      await refresh();
    },
    onError: (err) => toast.error(authMessage(err)),
  });
  const removeGrant = useMutation({
    mutationFn: (uid: string) => api.removeGrant(uid),
    onSuccess: refresh,
    onError: (err) => toast.error(authMessage(err)),
  });

  const people = data.data?.people ?? [];
  const listed = people.filter((p) => p.source !== "default");
  const addable = people.filter((p) => p.source === "default");
  const restricted = data.data?.restricted ?? false;
  const fallback = restricted ? "none" : (data.data?.org_default ?? "none");

  return (
    <section
      id="settings-access"
      aria-label="Access"
      className="flex scroll-mt-4 flex-col gap-4 rounded-xl border border-border bg-card p-5"
      data-testid="project-access"
    >
      <div>
        <h2 className="text-sm font-semibold">Access</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Org admins and owners always have the Admin role. Everyone else gets the role given here, or the
          organization's default.
        </p>
      </div>
      {data.isLoading && <Skeleton className="h-24" />}
      {data.isError && <p className="text-sm text-destructive">Failed to load: {data.error.message}</p>}
      {data.data && (
        <>
          <label className="flex items-center gap-3 text-sm">
            <Switch
              aria-label="Restricted"
              checked={restricted}
              disabled={restrict.isPending}
              onCheckedChange={(v) => restrict.mutate(v)}
            />
            <span className="flex flex-col leading-tight">
              <span className="font-medium">Restricted</span>
              <span className="text-xs text-muted-foreground">
                Only org admins and people given access below can open this project.
              </span>
            </span>
          </label>
          <ul className="divide-y divide-border" data-testid="access-people">
            {listed.map((p) => (
              <li key={p.uid} className="flex flex-wrap items-center gap-3 py-2.5" data-testid={`access-${p.uid}`}>
                <UserAvatar name={personName(p)} url={p.avatar} />
                <div className="flex min-w-0 flex-1 flex-col leading-tight">
                  <span className="truncate font-medium">{personName(p)}</span>
                  <span className="truncate font-mono text-xs text-muted-foreground">
                    {p.login ? `@${p.login}` : ""}
                  </span>
                </div>
                {typeof p.month_spend_usd === "number" && (
                  <span className="text-xs text-muted-foreground" data-testid={`access-spend-${p.uid}`}>
                    {formatUsd(p.month_spend_usd)} this month
                  </span>
                )}
                {p.source === "org_admin" ? (
                  <>
                    <Badge variant="secondary">Admin</Badge>
                    <span className="text-xs text-muted-foreground">as org {p.org_role}</span>
                  </>
                ) : (
                  <>
                    <select
                      aria-label={`Role for ${personName(p)}`}
                      className={`${nativeSelectClass} w-32`}
                      value={p.project_role ?? "viewer"}
                      disabled={setGrant.isPending}
                      onChange={(e) => setGrant.mutate({ uid: p.uid, role: e.target.value as ProjectRole })}
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={removeGrant.isPending}
                      onClick={() => removeGrant.mutate(p.uid)}
                    >
                      Remove
                    </Button>
                  </>
                )}
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground" data-testid="access-default">
            Everyone else:{" "}
            {restricted
              ? "no access (Restricted)"
              : fallback === "none"
                ? "no access (the organization's default)"
                : `${DEFAULT_LABEL[fallback]} (the organization's default)`}
            .
          </p>
          {addable.length > 0 && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-end" data-testid="access-add">
              <select
                aria-label="Add person"
                className={nativeSelectClass}
                value={addUid}
                onChange={(e) => setAddUid(e.target.value)}
              >
                <option value="">Add person...</option>
                {addable.map((p) => (
                  <option key={p.uid} value={p.uid}>
                    {personName(p)}
                  </option>
                ))}
              </select>
              <select
                aria-label="Role for the new person"
                className={`${nativeSelectClass} sm:w-36`}
                value={addRole}
                onChange={(e) => setAddRole(e.target.value as ProjectRole)}
              >
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <Button
                disabled={!addUid || setGrant.isPending}
                onClick={() => setGrant.mutate({ uid: addUid, role: addRole })}
              >
                Add
              </Button>
            </div>
          )}
          {data.data.invitations.length > 0 && (
            <div className="flex flex-col gap-1" data-testid="access-invitations">
              <h3 className="text-xs font-medium">Invited, not signed in yet</h3>
              <ul className="text-xs text-muted-foreground">
                {data.data.invitations.map((i) => (
                  <li key={i.uid}>
                    <span className="font-mono">@{i.github_login}</span> - {i.role}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}
