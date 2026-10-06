import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ApiError,
  invitationsApi,
  membersApi,
  portalApi,
  portalKey,
  type Invitation,
  type Member,
  type MemberRole,
  type PendingInvite,
  type ProjectRole,
} from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { CopyButton } from "../components/portal/CopyButton";
import { Field, nativeSelectClass } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";
import { authMessage } from "../lib/authErrors";
import { canAdmin, canOwn, usePortalState, useRole } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

const MEMBERS = portalKey("members");
const INVITATIONS = portalKey("invitations");

/** The roles a viewer may hand out: an admin gives `member` / `admin`, an owner also `owner`. */
function rolesFor(viewerIsOwner: boolean): MemberRole[] {
  return viewerIsOwner ? ["member", "admin", "owner"] : ["member", "admin"];
}

function joined(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleDateString();
}

const ROLE_HELP: Record<MemberRole, string> = {
  member: "Gets the organization's default project role, plus any project access given below.",
  admin: "Manages members, settings and every project.",
  owner: "Everything an admin does, plus ownership transfer, the audit log and deleting the organization.",
};

const GRANT_ROLES: ProjectRole[] = ["viewer", "contributor", "admin"];

/** The invite form: one GitHub username, a role and (for a member) optional project access. */
function InviteMember({ viewerIsOwner }: { viewerIsOwner: boolean }) {
  const queryClient = useQueryClient();
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<MemberRole>("member");
  const [grants, setGrants] = useState<Record<string, ProjectRole>>({});
  const [pending, setPending] = useState<PendingInvite | null>(null);
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const list = projects.data?.projects ?? [];
  const invite = useMutation({
    mutationFn: () => {
      const chosen = role === "member" ? Object.entries(grants).map(([project, r]) => ({ project, role: r })) : [];
      return membersApi.add({
        github_login: login.trim(),
        role,
        ...(chosen.length > 0 ? { grants: chosen } : {}),
      });
    },
    onSuccess: async (res) => {
      if ("pending" in res && res.pending) {
        setPending(res);
        await queryClient.invalidateQueries({ queryKey: INVITATIONS });
      } else {
        setPending(null);
        const m = res as Member;
        toast.success(`Added ${m.github_login ? `@${m.github_login}` : m.display_name}`);
        await queryClient.invalidateQueries({ queryKey: MEMBERS });
      }
      setLogin("");
      setRole("member");
      setGrants({});
    },
  });
  const code = invite.error instanceof ApiError ? invite.error.code : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setPending(null);
    invite.mutate();
  };

  return (
    <form
      noValidate
      onSubmit={submit}
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
      data-testid="add-member"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-sm font-semibold">Invite someone</h2>
        <p className="text-xs text-muted-foreground">
          Someone who has signed in is added at once. Anyone else is invited by GitHub username and
          joins the next time they sign in.
        </p>
      </div>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <Field
          label="GitHub username"
          className="flex-1"
          error={code === "bad_login" ? authMessage(invite.error) : undefined}
        >
          {(p) => (
            <Input
              {...p}
              autoComplete="off"
              spellCheck={false}
              placeholder="octocat"
              value={login}
              onChange={(e) => setLogin(e.target.value)}
            />
          )}
        </Field>
        <Field label="Role" className="sm:w-36">
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={role}
              onChange={(e) => setRole(e.target.value as MemberRole)}
            >
              {rolesFor(viewerIsOwner).map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      <p className="text-xs text-muted-foreground" data-testid="role-help">
        {ROLE_HELP[role]}
      </p>
      {role === "member" && list.length > 0 && (
        <fieldset className="flex flex-col gap-2" data-testid="invite-grants">
          <legend className="text-xs font-medium">Project access (optional)</legend>
          {list.map((proj) => (
            <div key={proj.slug} className="flex items-center gap-3">
              <span className="min-w-0 flex-1 truncate text-sm">{proj.name}</span>
              <select
                aria-label={`Access to ${proj.name}`}
                className={`${nativeSelectClass} w-36`}
                value={grants[proj.slug] ?? ""}
                onChange={(e) =>
                  setGrants((g) => {
                    const { [proj.slug]: _drop, ...rest } = g;
                    return e.target.value ? { ...rest, [proj.slug]: e.target.value as ProjectRole } : rest;
                  })
                }
              >
                <option value="">Organization default</option>
                {GRANT_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </fieldset>
      )}
      {invite.isError && code !== "bad_login" && (
        <Alert variant="destructive" data-testid="add-member-error">
          <AlertDescription>{authMessage(invite.error)}</AlertDescription>
        </Alert>
      )}
      {pending && (
        <Alert data-testid="invite-pending">
          <AlertDescription className="flex flex-wrap items-center gap-3">
            <span>
              No message is sent. Share this link with @{pending.github_login}:{" "}
              <span className="font-mono">{window.location.origin}</span>
            </span>
            <CopyButton text={window.location.origin} />
          </AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" disabled={invite.isPending || !login.trim()}>
          {invite.isPending ? "Inviting…" : "Invite"}
        </Button>
      </div>
    </form>
  );
}

/** Open invitations (and the last 30 days of closed ones) with Revoke on the open ones. */
function Invitations({ viewerIsOwner }: { viewerIsOwner: boolean }) {
  const queryClient = useQueryClient();
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const invitations = useQuery({ queryKey: INVITATIONS, queryFn: invitationsApi.list });
  const revoke = useMutation({
    mutationFn: (uid: string) => invitationsApi.revoke(uid),
    onSuccess: async () => {
      setRevoking(null);
      toast.success("Invitation revoked");
      await queryClient.invalidateQueries({ queryKey: INVITATIONS });
    },
    onError: (err) => setError(authMessage(err)),
  });
  const rows = invitations.data ?? [];
  if (!invitations.isLoading && rows.length === 0) return null;
  return (
    <section className="flex flex-col gap-1 rounded-xl border border-border bg-card p-5" data-testid="invitations">
      <h2 className="text-sm font-semibold">Invitations</h2>
      {invitations.isLoading && <Skeleton className="h-12" />}
      <ul className="divide-y divide-border">
        {rows.map((inv) => (
          <li key={inv.uid} className="flex flex-wrap items-center gap-3 py-3" data-testid={`invitation-${inv.uid}`}>
            <div className="flex min-w-0 flex-1 flex-col leading-tight">
              <span className="truncate font-mono text-sm">@{inv.github_login}</span>
              <span className="truncate text-xs text-muted-foreground">
                {inv.invited_by ? `Invited by ${inv.invited_by.display_name}` : "Invited"}
                {inv.grants.length > 0 && ` - access to ${inv.grants.map((g) => `${g.project} (${g.role})`).join(", ")}`}
              </span>
            </div>
            <Badge variant={inv.role === "member" ? "outline" : "secondary"}>{inv.role}</Badge>
            <span className="text-xs text-muted-foreground">
              {inv.status === "open" ? `Expires ${joined(inv.expires_at)}` : inv.status}
            </span>
            {(inv.status === "open" || inv.status === "expired") && (inv.role !== "owner" || viewerIsOwner) && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setError(null);
                  setRevoking(inv);
                }}
              >
                Revoke
              </Button>
            )}
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(o) => !o && setRevoking(null)}
        title={`Revoke the invitation for @${revoking?.github_login ?? ""}?`}
        description="The invitation stops working. You can invite them again."
        confirmLabel="Revoke"
        pending={revoke.isPending}
        error={error}
        onConfirm={() => revoking && revoke.mutate(revoking.uid)}
      />
    </section>
  );
}

/** One member: who, when, and (when the viewer may) a role select and Remove. */
function MemberRow({
  member,
  isMe,
  editable,
  viewerIsOwner,
}: {
  member: Member;
  isMe: boolean;
  editable: boolean;
  viewerIsOwner: boolean;
}) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const name = member.display_name || member.github_login || member.uid;

  const setRole = useMutation({
    mutationFn: (role: MemberRole) => membersApi.setRole(member.uid, role),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: MEMBERS });
      // Your own role decides what this page (and the rest of the org) offers.
      if (isMe) await queryClient.invalidateQueries({ queryKey: portalKey("state") });
    },
    onError: (err) => toast.error(authMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => membersApi.remove(member.uid),
    onSuccess: async () => {
      setConfirm(false);
      toast.success(`Removed ${name}`);
      await queryClient.invalidateQueries({ queryKey: MEMBERS });
    },
    onError: (err) => setRemoveError(authMessage(err)),
  });

  return (
    <li className="flex flex-wrap items-center gap-3 py-3" data-testid={`member-${member.uid}`}>
      <UserAvatar name={name} url={member.avatar_url} />
      <div className="flex min-w-0 flex-1 flex-col leading-tight">
        <span className="truncate font-medium">
          {name}
          {isMe && <span className="font-normal text-muted-foreground"> (you)</span>}
        </span>
        <span className="truncate font-mono text-xs text-muted-foreground">
          {member.github_login ? `@${member.github_login}` : "no GitHub account"}
        </span>
      </div>
      {member.disabled && <Badge variant="outline">disabled</Badge>}
      <span className="text-xs text-muted-foreground">Joined {joined(member.joined_at)}</span>
      {editable ? (
        <select
          aria-label={`Role for ${name}`}
          className={`${nativeSelectClass} w-28`}
          value={member.role}
          disabled={setRole.isPending}
          onChange={(e) => setRole.mutate(e.target.value as MemberRole)}
        >
          {rolesFor(viewerIsOwner).map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      ) : (
        <Badge variant={member.role === "member" ? "outline" : "secondary"}>{member.role}</Badge>
      )}
      {editable && !isMe && (
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            setRemoveError(null);
            setConfirm(true);
          }}
        >
          Remove
        </Button>
      )}
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Remove ${name}?`}
        description="They lose access to this organization's projects at once. Their chat history is kept and comes back if they are added again."
        confirmLabel="Remove"
        pending={remove.isPending}
        error={removeError}
        onConfirm={() => remove.mutate()}
      />
    </li>
  );
}

/**
 * `/members` on an org host (production only): everyone in the org. Admins add,
 * re-role and remove members and admins; only owners make or touch an owner.
 * Every member can leave, except the last owner.
 */
export function MembersPage() {
  const state = usePortalState().data;
  const role = useRole();
  const admin = canAdmin(role);
  const owner = canOwn(role);
  const me = state?.user?.uid;
  const members = useQuery({ queryKey: MEMBERS, queryFn: membersApi.list });
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);

  const owners = members.data?.filter((m) => m.role === "owner").length ?? 0;
  const lastOwner = owner && owners <= 1;
  // A reader (an instance admin outside the org) is not a member, so has nothing to leave.
  const canLeave = !!role && role !== "reader" && !lastOwner && members.isSuccess;

  const leave = useMutation({
    mutationFn: membersApi.leave,
    onSuccess: () => {
      const base = state?.base_url?.replace(/\/$/, "") ?? "";
      return hardNavigate(`${base}/orgs`);
    },
    onError: (err) => setLeaveError(authMessage(err)),
  });

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6 sm:p-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Members</h1>
        <p className="text-[13px] text-muted-foreground">
          Everyone who can open {state?.org?.name ?? "this organization"}'s projects.
        </p>
      </div>

      {admin && <InviteMember viewerIsOwner={owner} />}
      {admin && <Invitations viewerIsOwner={owner} />}

      <section className="flex flex-col gap-1 rounded-xl border border-border bg-card p-5">
        {members.isLoading && <Skeleton className="h-16" />}
        {members.isError && (
          <p className="text-sm text-destructive">Failed to load: {members.error.message}</p>
        )}
        {members.data && (
          <ul className="divide-y divide-border" data-testid="member-list">
            {members.data.map((m) => (
              <MemberRow
                key={m.uid}
                member={m}
                isMe={m.uid === me}
                editable={admin && (m.role !== "owner" || owner)}
                viewerIsOwner={owner}
              />
            ))}
          </ul>
        )}
      </section>

      {canLeave && (
        <div>
          <Button
            variant="outline"
            onClick={() => {
              setLeaveError(null);
              setLeaving(true);
            }}
          >
            Leave organization
          </Button>
          <ConfirmDialog
            open={leaving}
            onOpenChange={setLeaving}
            title="Leave this organization?"
            description="You lose access to its projects at once. An admin or owner can add you again."
            confirmLabel="Leave organization"
            pending={leave.isPending || leave.isSuccess}
            error={leaveError}
            onConfirm={() => leave.mutate()}
          />
        </div>
      )}
    </div>
  );
}
