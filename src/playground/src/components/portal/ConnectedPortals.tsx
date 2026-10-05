import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { connectApi, type MyConnection } from "../../api";
import { revokedLabel } from "../../lib/connections";
import { linkError } from "../../lib/errors";
import { timeAgo } from "../../lib/projectStatus";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";

const MINE = ["@account", "connections"] as const;
const projectConnections = (slug: string) => ["@project", slug, "connections"] as const;

function When({ label, iso }: { label: string; iso: string | null }) {
  const ago = timeAgo(iso);
  return ago ? (
    <span>
      {label} {ago}
    </span>
  ) : null;
}

/**
 * Account -> Connected portals: the caller's own connection tokens, one per
 * local portal linked to a project (M2e plan section 4.4). A live one can be
 * revoked; a revoked one stays listed, with why, until it is swept.
 */
export function MyConnectedPortals() {
  const queryClient = useQueryClient();
  const list = useQuery({ queryKey: MINE, queryFn: connectApi.tokens });
  const revoke = useMutation({
    mutationFn: (uid: string) => connectApi.revokeToken(uid),
    onSuccess: async () => {
      toast.success("Connection revoked");
      await queryClient.invalidateQueries({ queryKey: MINE });
    },
    onError: (err) => toast.error(linkError(err)),
  });

  return (
    <section
      aria-label="Connected portals"
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5"
      data-testid="my-connections"
    >
      <div>
        <h2 className="text-sm font-semibold">Connected portals</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Local WhyGraph portals linked to a project with your account. Revoking one cuts that machine
          off at once; it can be linked again.
        </p>
      </div>
      {list.isLoading && <Skeleton className="h-12" />}
      {list.isError && <p className="text-sm text-destructive">{linkError(list.error)}</p>}
      {list.isSuccess && list.data.length === 0 && (
        <p className="text-sm text-muted-foreground">No local portal is connected.</p>
      )}
      {list.isSuccess && list.data.length > 0 && (
        <ul className="flex flex-col divide-y divide-border">
          {list.data.map((c: MyConnection) => {
            const live = c.revoked_at === null;
            return (
              <li key={c.uid} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm" data-testid="connection-row">
                <div className="flex min-w-0 flex-col">
                  <span className="font-medium">{c.client_name}</span>
                  <span className="text-xs text-muted-foreground">
                    <span className="font-mono">
                      {c.org && c.project ? `${c.org}/${c.project}` : (c.project_name ?? "a deleted project")}
                    </span>
                    {" · "}
                    <When label="linked" iso={c.created_at} />
                    {c.last_used_at && " · "}
                    <When label="last used" iso={c.last_used_at} />
                  </span>
                </div>
                {live ? (
                  <Button
                    size="sm"
                    variant="outline"
                    className="ml-auto"
                    disabled={revoke.isPending}
                    onClick={() => revoke.mutate(c.uid)}
                  >
                    Revoke
                  </Button>
                ) : (
                  <Badge variant="outline" className="ml-auto">
                    {revokedLabel(c.revoked_reason)}
                  </Badge>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/**
 * Project settings -> Connected portals (owners and admins): every member's live
 * connection to this project, with the machine name each gave. An admin may
 * revoke any of them.
 */
export function ProjectConnectedPortals({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const key = projectConnections(slug);
  const list = useQuery({ queryKey: key, queryFn: () => connectApi.projectConnections(slug) });
  const revoke = useMutation({
    mutationFn: (uid: string) => connectApi.revokeProjectConnection(slug, uid),
    onSuccess: async () => {
      toast.success("Connection revoked");
      await queryClient.invalidateQueries({ queryKey: key });
    },
    onError: (err) => toast.error(linkError(err)),
  });

  return (
    <section
      id="settings-connections"
      aria-label="Connected portals"
      className="flex scroll-mt-4 flex-col gap-3 rounded-xl border border-border bg-card p-5"
      data-testid="project-connections"
    >
      <div>
        <h2 className="text-sm font-semibold">Connected portals</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Members' local portals linked to this project. Revoking one cuts that machine off at once.
        </p>
      </div>
      {list.isLoading && <Skeleton className="h-12" />}
      {list.isError && <p className="text-sm text-destructive">{linkError(list.error)}</p>}
      {list.isSuccess && list.data.length === 0 && (
        <p className="text-sm text-muted-foreground">No local portal is connected to this project.</p>
      )}
      {list.isSuccess && list.data.length > 0 && (
        <ul className="flex flex-col divide-y divide-border">
          {list.data.map((c) => (
            <li key={c.uid} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm" data-testid="connection-row">
              <div className="flex min-w-0 flex-col">
                <span className="font-medium">{c.client_name}</span>
                <span className="text-xs text-muted-foreground">
                  {c.user_name ?? c.user_login ?? "A member"}
                  {c.user_login && c.user_name ? <span className="font-mono"> @{c.user_login}</span> : null}
                  {" · "}
                  <When label="linked" iso={c.created_at} />
                  {c.last_used_at && " · "}
                  <When label="last used" iso={c.last_used_at} />
                </span>
              </div>
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                disabled={revoke.isPending}
                onClick={() => revoke.mutate(c.uid)}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
