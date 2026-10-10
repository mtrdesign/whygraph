import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { connectApi, type ConnectionRow, type MyConnection } from "../../api";
import { revokedLabel, revokedLabelForAdmin } from "../../lib/connections";
import { linkError } from "../../lib/errors";
import { timeAgo } from "../../lib/projectStatus";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { SettingsSection } from "../settings/SettingsLayout";
import { ErrorState } from "../state/ErrorState";
import { ConfirmDialog } from "./ConfirmDialog";

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
 * revoke any of them (after a confirmation). Tokens revoked in the last 30 days
 * stay listed below, with why (SET-10).
 */
export function ProjectConnectedPortals({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const key = projectConnections(slug);
  const list = useQuery({
    queryKey: key,
    queryFn: () => connectApi.projectConnections(slug, { includeRevoked: true }),
  });
  const [revoking, setRevoking] = useState<ConnectionRow | null>(null);
  const revoke = useMutation({
    mutationFn: (uid: string) => connectApi.revokeProjectConnection(slug, uid),
    onSuccess: async () => {
      setRevoking(null);
      await queryClient.invalidateQueries({ queryKey: key });
    },
  });
  const live = (list.data ?? []).filter((c) => !c.revoked_at);
  const revoked = (list.data ?? []).filter((c) => !!c.revoked_at);
  const who = (c: ConnectionRow) => (
    <>
      {c.user_name ?? c.user_login ?? "A member"}
      {c.user_login && c.user_name ? <span className="font-mono"> @{c.user_login}</span> : null}
    </>
  );

  return (
    <SettingsSection
      id="connections"
      title="Connected portals"
      testId="project-connections"
      description="Members' local portals linked to this project. Revoking one cuts that machine off at once."
    >
      {list.isLoading && <Skeleton className="h-12" />}
      {list.isError && (
        <ErrorState error={list.error} context="link" title="Couldn't load the connected portals" onRetry={() => void list.refetch()} />
      )}
      {list.isSuccess && live.length === 0 && (
        <p className="text-sm text-muted-foreground">No local portal is connected to this project.</p>
      )}
      {live.length > 0 && (
        <ul className="flex flex-col divide-y divide-border">
          {live.map((c) => (
            <li key={c.uid} className="row-wrap py-2 text-sm" data-testid="connection-row">
              <div className="flex min-w-0 flex-1 flex-col">
                <span className="font-medium break-words">{c.client_name}</span>
                <span className="text-xs text-muted-foreground">
                  {who(c)}
                  {" · "}
                  <When label="linked" iso={c.created_at} />
                  {c.last_used_at && " · "}
                  <When label="last used" iso={c.last_used_at} />
                </span>
              </div>
              <Button
                size="sm"
                variant="outline"
                disabled={revoke.isPending}
                onClick={() => {
                  revoke.reset();
                  setRevoking(c);
                }}
              >
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
      {revoked.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="revoked-connections">
          <h3 className="text-xs font-medium">Revoked (last 30 days)</h3>
          <ul className="flex flex-col divide-y divide-border">
            {revoked.map((c) => (
              <li key={c.uid} className="row-wrap py-2 text-sm" data-testid="revoked-connection-row">
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="break-words text-muted-foreground">{c.client_name}</span>
                  <span className="text-xs text-muted-foreground">
                    {who(c)}
                    {" · "}
                    <When label="revoked" iso={c.revoked_at ?? null} />
                  </span>
                </div>
                <Badge variant="outline">{revokedLabelForAdmin(c.revoked_reason ?? null)}</Badge>
              </li>
            ))}
          </ul>
        </div>
      )}
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => {
          if (!open) setRevoking(null);
        }}
        title={`Revoke ${revoking?.client_name ?? "this connection"}?`}
        description="That machine loses access to this project at once. Its owner can link it again."
        confirmLabel="Revoke"
        pending={revoke.isPending}
        error={revoke.isError ? linkError(revoke.error) : null}
        onConfirm={() => revoking && revoke.mutate(revoking.uid)}
      />
    </SettingsSection>
  );
}
