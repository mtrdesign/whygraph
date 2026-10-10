import { PageContainer } from "../components/layout/PageContainer";
import { useState, type FormEvent } from "react";
import { Link } from "@tanstack/react-router";
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
  type ProjectRole,
} from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { CommandBlock } from "../components/layout/CommandBlock";
import { Field, nativeSelect, nativeSelectClass } from "../components/portal/Field";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { ErrorState } from "../components/state/ErrorState";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";
import { authMessage } from "../lib/authErrors";
import { formatDate, formatUsd } from "../lib/format";
import { canAdmin, canOwn, usePortalState, useRole } from "../lib/identity";
import { orgRoleLabel, projectRoleLabel } from "../lib/labels";
import { hardNavigate } from "../lib/navigation";

const MEMBERS = portalKey("members");
const INVITATIONS = portalKey("invitations");

/** The roles a viewer may hand out: an admin gives `member` / `admin`, an owner also `owner`. */
function rolesFor(viewerIsOwner: boolean): MemberRole[] {
  return viewerIsOwner ? ["member", "admin", "owner"] : ["member", "admin"];
}

const ROLE_HELP: Record<MemberRole, string> = {
  member: "Gets the organization's default project role, plus any project access given below.",
  admin: "Manages members, settings and every project.",
  owner: "Everything an admin does, plus ownership transfer, the audit log and deleting the organization.",
};

const GRANT_ROLES: ProjectRole[] = ["viewer", "contributor", "admin"];

type Notice =
  | { kind: "invited"; key: string; login: string }
  | { kind: "added"; key: string; uid: string; login: string };

/** The invite form: one GitHub username, a role and (for a member) optional project access. */
function InviteMember({ viewerIsOwner }: { viewerIsOwner: boolean }) {
  const queryClient = useQueryClient();
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<MemberRole>("member");
  const [grants, setGrants] = useState<Record<string, ProjectRole>>({});
  // One notice per invite or direct add, kept until dismissed: the next submit does not clear them.
  const [notices, setNotices] = useState<Notice[]>([]);
  const orgName = usePortalState().data?.org?.name ?? "this organization";
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
        setNotices((n) => [...n, { kind: "invited", key: res.uid, login: res.github_login }]);
        await queryClient.invalidateQueries({ queryKey: INVITATIONS });
      } else {
        const m = res as Member;
        setNotices((n) => [
          ...n,
          { kind: "added", key: m.uid, uid: m.uid, login: m.github_login ?? m.display_name },
        ]);
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
    invite.mutate();
  };

  if (projects.isLoading) {
    // ER-5: the form appears whole once its data is in - no fieldset popping in.
    return (
      <div
        className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card"
        data-testid="add-member-loading"
        aria-busy="true"
      >
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-3 w-full max-w-md" />
        <div className="flex flex-col gap-3 sm:flex-row">
          <Skeleton className="h-8 flex-1" />
          <Skeleton className="h-8 sm:w-36" />
        </div>
        <Skeleton className="h-8 w-20" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
    <form
      noValidate
      onSubmit={submit}
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card"
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
                  {orgRoleLabel(r)}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      <p className="text-xs text-muted-foreground" data-testid="role-help">
        {ROLE_HELP[role]}
      </p>
      {role === "member" && projects.isError && (
        <ErrorState
          error={projects.error}
          title="Couldn't load the projects for project access"
          onRetry={() => void projects.refetch()}
        />
      )}
      {role === "member" && list.length > 0 && (
        <fieldset className="flex flex-col gap-2" data-testid="invite-grants">
          <legend className="text-xs font-medium">Project access (optional)</legend>
          {list.map((proj) => (
            <div key={proj.slug} className="flex items-center gap-3">
              <span className="min-w-0 flex-1 truncate text-sm" title={proj.name}>
                {proj.name}
              </span>
              {/* Wide enough for "Organization default". */}
              <select
                aria-label={`Access to ${proj.name}`}
                className={nativeSelect("w-48 shrink-0")}
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
                    {projectRoleLabel(r)}
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
      <div>
        <Button type="submit" disabled={invite.isPending || !login.trim()}>
          {invite.isPending ? "Inviting…" : "Invite"}
        </Button>
      </div>
    </form>
      {notices.map((n) => (
        <Alert key={`${n.kind}-${n.key}`} variant="info" data-testid={n.kind === "invited" ? "invite-pending" : "member-added"}>
          {/* Dismiss sits in the flow (no reserved right column), so the text has
              the whole width on a phone and Dismiss wraps under it. */}
          <AlertDescription className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
            {n.kind === "invited" ? (
              <div className="flex min-w-0 flex-[1_1_16rem] flex-col gap-2">
                <span>No message is sent. Share this link with @{n.login}:</span>
                <CommandBlock command={window.location.origin} />
              </div>
            ) : (
              <span className="min-w-0 flex-[1_1_16rem]">
                @{n.login} is now a member. They'll see a welcome note the next time they open {orgName}.{" "}
                <a href={`#member-${n.uid}`} className="text-primary-text hover:underline">
                  Show in the list
                </a>
              </span>
            )}
            <Button
              size="sm"
              variant="ghost"
              className="shrink-0"
              onClick={() => setNotices((list) => list.filter((x) => x !== n))}
            >
              Dismiss
            </Button>
          </AlertDescription>
        </Alert>
      ))}
    </div>
  );
}

/** What an invitation's status column says. */
function inviteStatus(inv: Invitation): string {
  switch (inv.status) {
    case "open":
      return `Open, expires ${formatDate(inv.expires_at)}`;
    case "expired":
      return `Expired ${formatDate(inv.expires_at)}`;
    case "redeemed":
      return `Accepted ${formatDate(inv.redeemed_at)}`;
    default:
      return `Revoked ${formatDate(inv.revoked_at)}`;
  }
}

/**
 * Open and expired invitations with Revoke on them; the ones closed in the last
 * 30 days (accepted or revoked) sit under a collapsed "Closed" heading (MEM-4).
 */
function Invitations({ viewerIsOwner }: { viewerIsOwner: boolean }) {
  const queryClient = useQueryClient();
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showClosed, setShowClosed] = useState(false);
  const invitations = useQuery({ queryKey: INVITATIONS, queryFn: invitationsApi.list });
  const revoke = useMutation({
    mutationFn: (uid: string) => invitationsApi.revoke(uid),
    onSuccess: async () => {
      setRevoking(null);
      await queryClient.invalidateQueries({ queryKey: INVITATIONS });
    },
    onError: (err) => setError(authMessage(err)),
  });
  const rows = invitations.data ?? [];
  const current = rows.filter((i) => i.status === "open" || i.status === "expired");
  const closed = rows.filter((i) => i.status !== "open" && i.status !== "expired");
  // Hidden only when there truly are none; a failed load says so, with Retry (ER-4).
  if (invitations.isSuccess && rows.length === 0) return null;

  const columns = (withActions: boolean): Column<Invitation>[] => [
    {
      key: "who",
      header: "Person",
      primary: true,
      cell: (inv) => <span className="font-mono">@{inv.github_login}</span>,
    },
    {
      key: "role",
      header: "Role",
      cell: (inv) => <Badge variant={inv.role === "member" ? "outline" : "secondary"}>{orgRoleLabel(inv.role)}</Badge>,
    },
    {
      key: "by",
      header: "Invited by",
      hideBelow: "md",
      cell: (inv) => inv.invited_by?.display_name ?? "-",
    },
    {
      key: "access",
      header: "Project access",
      hideBelow: "md",
      cell: (inv) =>
        inv.grants.length > 0 ? (
          <span className="flex flex-wrap gap-1">
            {inv.grants.map((g) => (
              <Badge key={g.project} variant="outline">
                {g.project}: {projectRoleLabel(g.role)}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="text-muted-foreground">Organization default</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      cell: (inv) => <span className="text-muted-foreground">{inviteStatus(inv)}</span>,
    },
    ...(withActions
      ? [
          {
            key: "actions",
            header: <span className="sr-only">Actions</span>,
            cell: (inv: Invitation) =>
              inv.role !== "owner" || viewerIsOwner ? (
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
              ) : null,
          } satisfies Column<Invitation>,
        ]
      : []),
  ];

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card" data-testid="invitations">
      <h2 className="text-sm font-semibold">Invitations</h2>
      {invitations.isLoading && <Skeleton className="h-12" />}
      {invitations.isError && (
        <ErrorState
          error={invitations.error}
          title="Couldn't load the invitations"
          onRetry={() => void invitations.refetch()}
        />
      )}
      {current.length > 0 && (
        <ResponsiveTable
          columns={columns(true)}
          rows={current}
          rowKey={(i) => i.uid}
          rowTestId={(i) => `invitation-${i.uid}`}
        />
      )}
      {invitations.isSuccess && current.length === 0 && (
        <p className="text-sm text-muted-foreground">No open invitations.</p>
      )}
      {closed.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="invitations-closed">
          <button
            type="button"
            className="w-fit text-xs font-medium text-primary-text hover:underline"
            aria-expanded={showClosed}
            onClick={() => setShowClosed((v) => !v)}
          >
            {showClosed ? "Hide" : "Show"} closed invitations (last 30 days, {closed.length})
          </button>
          {showClosed && (
            <ResponsiveTable
              columns={columns(false)}
              rows={closed}
              rowKey={(i) => i.uid}
              rowTestId={(i) => `invitation-${i.uid}`}
            />
          )}
        </div>
      )}
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
      await queryClient.invalidateQueries({ queryKey: MEMBERS });
    },
    onError: (err) => setRemoveError(authMessage(err)),
  });

  return (
    <li className="row-wrap py-3 sm:flex-nowrap sm:[&>*:not([data-testid=member-identity])]:shrink-0" id={`member-${member.uid}`} data-testid={`member-${member.uid}`}>
      {/* The identity takes the whole first line on a phone (the details and
          actions wrap under it), never squeezed beside them. */}
      <div className="flex min-w-0 basis-full items-center gap-3 sm:basis-0 sm:flex-1" data-testid="member-identity">
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
      </div>
      {member.disabled && <Badge variant="outline">disabled</Badge>}
      {member.grants && member.grants.length > 0 && (
        <span className="flex flex-wrap gap-1" data-testid={`member-grants-${member.uid}`}>
          {member.grants.map((g) => (
            <Badge key={g.project} variant="outline" title={`Access to ${g.name}`}>
              {g.name}: {projectRoleLabel(g.role)}
            </Badge>
          ))}
        </span>
      )}
      <span className="text-xs text-muted-foreground">Joined {formatDate(member.joined_at)}</span>
      {typeof member.month_spend_usd === "number" && (
        <Link
          to="/usage/members/$uid"
          params={{ uid: member.uid }}
          className="text-xs text-primary-text hover:underline"
          data-testid={`member-spend-${member.uid}`}
          aria-label={`This month for ${name}`}
          title="This month"
        >
          This month: {formatUsd(member.month_spend_usd)}
        </Link>
      )}
      {editable && !isMe ? (
        <select
          aria-label={`Role for ${name}`}
          className={nativeSelect("w-28")}
          value={member.role}
          disabled={setRole.isPending}
          onChange={(e) => setRole.mutate(e.target.value as MemberRole)}
        >
          {rolesFor(viewerIsOwner).map((r) => (
            <option key={r} value={r}>
              {orgRoleLabel(r)}
            </option>
          ))}
        </select>
      ) : (
        isMe ? (
          <>
            {/* Your own role is text: the server refuses a self re-role, so none is offered (BUG-25). */}
            <span className="text-sm" data-testid="own-role">
              {orgRoleLabel(member.role)}
            </span>
            {editable && (
              <span className="text-xs text-muted-foreground" data-testid="own-role-note">
                You can't change your own role
              </span>
            )}
          </>
        ) : (
          <Badge variant={member.role === "member" ? "outline" : "secondary"}>{member.role}</Badge>
        )
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
    <PageContainer width="narrow" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Members</h1>
        <p className="text-[13px] text-muted-foreground">
          Everyone who can open {state?.org?.name ?? "this organization"}'s projects.
        </p>
      </div>

      {admin && <InviteMember viewerIsOwner={owner} />}
      {admin && <Invitations viewerIsOwner={owner} />}

      <section className="flex flex-col gap-1 rounded-xl border border-border bg-card p-5 shadow-card">
        <h2 className="text-sm font-semibold" data-testid="members-heading">
          Members{members.data ? ` (${members.data.length})` : ""}
        </h2>
        {members.isLoading && <Skeleton className="h-16" />}
        {members.isError && (
          <ErrorState error={members.error} title="Couldn't load the members" onRetry={() => void members.refetch()} />
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
    </PageContainer>
  );
}
