import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, membersApi, portalKey, type Member, type MemberRole } from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { Field, nativeSelectClass } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";
import { authMessage } from "../lib/authErrors";
import { canAdmin, canOwn, usePortalState, useRole } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

const MEMBERS = portalKey("members");

/** The roles a viewer may hand out: an admin gives `member` / `admin`, an owner also `owner`. */
function rolesFor(viewerIsOwner: boolean): MemberRole[] {
  return viewerIsOwner ? ["member", "admin", "owner"] : ["member", "admin"];
}

function joined(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleDateString();
}

/** A yes / no question before a destructive call; the error stays in the dialog. */
function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pending,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  pending: boolean;
  error: string | null;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="destructive" disabled={pending} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The add form: one GitHub username and a role. */
function AddMember({ viewerIsOwner }: { viewerIsOwner: boolean }) {
  const queryClient = useQueryClient();
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<MemberRole>("member");
  const add = useMutation({
    mutationFn: () => membersApi.add({ github_login: login.trim(), role }),
    onSuccess: async (m) => {
      toast.success(`Added ${m.github_login ? `@${m.github_login}` : m.display_name}`);
      setLogin("");
      setRole("member");
      await queryClient.invalidateQueries({ queryKey: MEMBERS });
    },
  });
  const code = add.error instanceof ApiError ? add.error.code : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    add.mutate();
  };

  return (
    <form
      noValidate
      onSubmit={submit}
      className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
      data-testid="add-member"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-sm font-semibold">Add member</h2>
        <p className="text-xs text-muted-foreground">
          They must have signed in to WhyGraph with GitHub at least once.
        </p>
      </div>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <Field
          label="GitHub username"
          className="flex-1"
          error={code === "bad_login" ? authMessage(add.error) : undefined}
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
      {add.isError && code !== "bad_login" && (
        <Alert variant="destructive" data-testid="add-member-error">
          <AlertDescription>{authMessage(add.error)}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" disabled={add.isPending || !login.trim()}>
          {add.isPending ? "Adding…" : "Add member"}
        </Button>
      </div>
    </form>
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

      {admin && <AddMember viewerIsOwner={owner} />}

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
