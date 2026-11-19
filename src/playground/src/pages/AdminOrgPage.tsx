import { useState, type FormEvent } from "react";
import { useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  adminApi,
  type AdminMe,
  type AdminOrgDetail,
  type AdminOrgMember,
  type AdminOrgProject,
  type MemberRole,
} from "../api";
import { UserAvatar } from "../components/auth/UserAvatar";
import { AdminHeading } from "../components/admin/AdminHeading";
import {
  ADMIN,
  AdminNotFound,
  DeletedAlert,
  hasStatus,
  toastError,
  userLabel,
} from "../components/admin/adminParts";
import { RenameOrgDialog, SuspendOrgDialog, NO_SUCH_ACCOUNT, findByLogin } from "../components/admin/OrgDialogs";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { TabsSelect } from "../components/layout/TabsSelect";
import { AuditTable, type AuditUrlFilters } from "../components/portal/AuditTable";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { Field, nativeSelect } from "../components/portal/Field";
import { TypedConfirmDialog } from "../components/portal/TypedConfirmDialog";
import { DisabledReason } from "../components/state/DisabledReason";
import { ErrorState } from "../components/state/ErrorState";
import { FormSkeleton } from "../components/state/Skeletons";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { StatTile } from "../components/usage/parts";
import { errorMessage } from "../lib/apiErrors";
import { formatClock, formatDate, formatUsd, formatNumber, timeAgo } from "../lib/format";
import { orgRoleLabel } from "../lib/labels";
import { plural } from "../lib/plural";
import { ADMIN_ORG_TABS, type AdminOrgSearch, type AdminOrgTab } from "../lib/routeSearch";

const CARD = "flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card";
const DESTRUCTIVE = "text-destructive";

const TAB_LABELS: Record<AdminOrgTab, string> = {
  overview: "Overview",
  members: "Members",
  projects: "Projects",
  usage: "Usage",
  audit: "Audit",
};

/**
 * "Open as reader" (§4.4): the POST makes the grant, then the button is replaced by a link, because a
 * `window.open` after an `await` is blocked as a popup by Safari and Firefox. Hidden for a password account.
 */
function OpenAsReader({ org, name, me }: { org: string; name: string; me: AdminMe | undefined }) {
  const queryClient = useQueryClient();
  const grant = useMutation({
    mutationFn: () => adminApi.readerGrant(org),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: [...ADMIN, "me"] }),
    onError: toastError,
  });
  if (!me || me.has_password) return null;
  if (grant.data) {
    return (
      <a
        href={grant.data.org_url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm font-medium text-primary-text hover:underline"
        data-testid="reader-link"
      >
        Open {name} (read-only until {formatClock(grant.data.expires_at)})
      </a>
    );
  }
  return (
    <Button variant="outline" disabled={grant.isPending} onClick={() => grant.mutate()}>
      Open as reader
    </Button>
  );
}

/** The header actions: Rename, Open as reader, Suspend / Unsuspend and Delete (a deleted org has only Restore and Delete permanently). */
function OrgActions({ org, me }: { org: AdminOrgDetail; me: AdminMe | undefined }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [renaming, setRenaming] = useState(false);
  const [suspending, setSuspending] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [purging, setPurging] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const unsuspend = useMutation({ mutationFn: () => adminApi.unsuspendOrg(org.slug), onSuccess: done, onError: toastError });
  const restore = useMutation({ mutationFn: () => adminApi.restoreOrg(org.slug), onSuccess: done, onError: toastError });
  const remove = useMutation({
    mutationFn: () => adminApi.deleteOrg(org.slug),
    onSuccess: async () => {
      setDeleting(false);
      await done();
    },
  });
  const purge = useMutation({
    mutationFn: () => adminApi.purgeOrg(org.slug),
    onSuccess: async () => {
      setPurging(false);
      await done();
      await navigate({ to: "/admin/orgs" });
    },
  });

  return (
    <div className="row-wrap" data-testid="org-actions">
      {org.deleted_at ? (
        <>
          <Button variant="secondary" disabled={restore.isPending} onClick={() => restore.mutate()}>
            Restore
          </Button>
          <Button
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              purge.reset();
              setPurging(true);
            }}
          >
            Delete permanently
          </Button>
        </>
      ) : (
        <>
          <Button variant="outline" onClick={() => setRenaming(true)}>
            Rename
          </Button>
          <OpenAsReader org={org.slug} name={org.name} me={me} />
          {org.suspended ? (
            <Button variant="outline" disabled={unsuspend.isPending} onClick={() => unsuspend.mutate()}>
              Unsuspend
            </Button>
          ) : (
            <Button variant="outline" onClick={() => setSuspending(true)}>
              Suspend
            </Button>
          )}
          <Button
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              remove.reset();
              setDeleting(true);
            }}
          >
            Delete
          </Button>
        </>
      )}
      <RenameOrgDialog slug={org.slug} name={org.name} open={renaming} onOpenChange={setRenaming} />
      <SuspendOrgDialog slug={org.slug} name={org.name} open={suspending} onOpenChange={setSuspending} />
      <TypedConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        testId="delete-org-dialog"
        title={`Delete ${org.name}?`}
        description={`${org.name} disappears for its ${plural(org.members.length, "member")} at once. Running scans are cancelled and its connection tokens are revoked. Nothing is erased: you can restore it, or delete it permanently later.`}
        expected={org.slug}
        confirmLabel="Delete organization"
        pendingLabel="Deleting..."
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
      <TypedConfirmDialog
        open={purging}
        onOpenChange={setPurging}
        testId="purge-org-dialog"
        title={`Delete ${org.name} permanently?`}
        description={`Its ${plural(org.projects.length, "project")} with their server copies and history, its members' access, settings and keys are deleted. This cannot be undone, and the name ${org.slug} can never be used again. The repositories on GitHub are not changed.`}
        expected={org.slug}
        confirmLabel="Delete permanently"
        pendingLabel="Deleting..."
        pending={purge.isPending}
        error={purge.isError ? errorMessage(purge.error) : null}
        onConfirm={() => purge.mutate()}
      />
    </div>
  );
}

function Overview({ org }: { org: AdminOrgDetail }) {
  return (
    <div className="flex flex-col gap-5" data-testid="org-overview">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Members" value={formatNumber(org.members.length)} testId="tile-members" />
        <StatTile
          label="Projects"
          value={formatNumber(org.projects.filter((p) => !p.deleted_at).length)}
          testId="tile-projects"
        />
        <StatTile label="Open invitations" value={formatNumber(org.open_invitations)} testId="tile-invitations" />
        <StatTile label="Spend this month" value={formatUsd(org.spend_mtd)} testId="tile-spend" />
      </div>
      <section className={CARD}>
        <h2 className="text-sm font-semibold">Details</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-muted-foreground">Address</dt>
          <dd className="break-all font-mono text-xs">{org.url}</dd>
          <dt className="text-muted-foreground">Created</dt>
          <dd>{formatDate(org.created_at)}</dd>
        </dl>
      </section>
      <section className={CARD}>
        <h2 className="text-sm font-semibold">Stored rows</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm" data-testid="stored-rows">
          <dt className="text-muted-foreground">Usage events</dt>
          <dd className="tabular-nums">{formatNumber(org.rows.usage_events)}</dd>
          <dt className="text-muted-foreground">Audit events</dt>
          <dd className="tabular-nums">{formatNumber(org.rows.audit_events)}</dd>
          <dt className="text-muted-foreground">Agent-call days</dt>
          <dd className="tabular-nums">{formatNumber(org.rows.agent_call_days)}</dd>
        </dl>
      </section>
    </div>
  );
}

/** One member row's role select (saves on change; a failure is a toast) and Remove. */
function MemberControls({ org, member, writable }: { org: AdminOrgDetail; member: AdminOrgMember; writable: boolean }) {
  const queryClient = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const setRole = useMutation({
    mutationFn: (role: MemberRole) => adminApi.setOrgMemberRole(org.slug, member.uid, role),
    onSuccess: done,
    onError: toastError,
  });
  const remove = useMutation({
    mutationFn: () => adminApi.removeOrgMember(org.slug, member.uid),
    onSuccess: async () => {
      setConfirm(false);
      await done();
    },
  });
  const name = member.display_name;
  if (!writable) return <span className="capitalize">{orgRoleLabel(member.role)}</span>;
  const roles: MemberRole[] =
    member.has_password && member.role !== "owner" ? ["member", "admin"] : ["member", "admin", "owner"];
  return (
    <span className="flex flex-wrap items-center gap-2">
      <select
        aria-label={`Role of ${name}`}
        className={nativeSelect("w-28")}
        value={member.role}
        disabled={setRole.isPending}
        onChange={(e) => setRole.mutate(e.target.value as MemberRole)}
      >
        {roles.map((r) => (
          <option key={r} value={r}>
            {orgRoleLabel(r)}
          </option>
        ))}
      </select>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          remove.reset();
          setConfirm(true);
        }}
      >
        Remove
      </Button>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Remove ${name} from ${org.name}?`}
        description="They lose access to its projects at once."
        confirmLabel="Remove"
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
    </span>
  );
}

/** Add by GitHub username: the login is resolved to a uid against the cached users list. */
function AddMember({ org }: { org: AdminOrgDetail }) {
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: [...ADMIN, "users"], queryFn: adminApi.users });
  const [login, setLogin] = useState("");
  const [role, setRole] = useState<MemberRole>("member");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: (uid: string) => adminApi.addOrgMember(org.slug, { uid, role }),
    onSuccess: async () => {
      setLogin("");
      await queryClient.invalidateQueries({ queryKey: ADMIN });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const user = findByLogin(users.data, login);
    if (!user) {
      setFieldError(NO_SUCH_ACCOUNT);
      return;
    }
    setFieldError(null);
    add.mutate(user.uid);
  };
  return (
    <form noValidate onSubmit={submit} className={CARD} data-testid="admin-add-member">
      <h2 className="text-sm font-semibold">Add member</h2>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <Field label="GitHub username" className="flex-1" error={fieldError ?? undefined}>
          {(p) => (
            <Input
              {...p}
              autoComplete="off"
              spellCheck={false}
              placeholder="octocat"
              value={login}
              onChange={(e) => {
                setLogin(e.target.value);
                setFieldError(null);
              }}
            />
          )}
        </Field>
        <Field label="Role" className="sm:w-36">
          {(p) => (
            <select {...p} className={nativeSelect()} value={role} onChange={(e) => setRole(e.target.value as MemberRole)}>
              {(["member", "admin", "owner"] as const).map((r) => (
                <option key={r} value={r}>
                  {orgRoleLabel(r)}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      {add.isError && (
        <Alert variant="destructive">
          <AlertDescription>{errorMessage(add.error)}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" disabled={add.isPending || !login.trim()}>
          Add
        </Button>
      </div>
    </form>
  );
}

function Members({ org, writable }: { org: AdminOrgDetail; writable: boolean }) {
  const columns: Column<AdminOrgMember>[] = [
    {
      key: "member",
      header: "Member",
      primary: true,
      cell: (m) => (
        <span className="flex min-w-0 items-center gap-2">
          <UserAvatar name={m.display_name} url={m.avatar_url} />
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="truncate font-medium">{m.display_name}</span>
            <span className="truncate font-mono text-xs text-muted-foreground">{userLabel({ ...m, email: null })}</span>
          </span>
        </span>
      ),
    },
    { key: "role", header: "Role", cell: (m) => <MemberControls org={org} member={m} writable={writable} /> },
    { key: "joined", header: "Joined", cell: (m) => formatDate(m.joined_at) },
  ];
  return (
    <div className="flex flex-col gap-4" data-testid="org-members">
      {writable && <AddMember org={org} />}
      <ResponsiveTable
        columns={columns}
        rows={org.members}
        rowKey={(m) => m.uid}
        rowTestId={(m) => `org-member-${m.uid}`}
        empty={<p className="text-sm text-muted-foreground">This organization has no members.</p>}
      />
    </div>
  );
}

function projectState(p: AdminOrgProject): { label: string; variant: "destructive" | "outline" | "secondary" } {
  if (p.deleted_at) return { label: "Deleted", variant: "destructive" };
  if (p.access_lost_reason) return { label: "Access lost", variant: "destructive" };
  if (p.last_run_status === "failed" || p.last_run_status === "interrupted") return { label: "Failing", variant: "destructive" };
  if (!p.last_scan_at) return { label: "Never scanned", variant: "outline" };
  return { label: "OK", variant: "secondary" };
}

/** An org's projects: a live one has Delete, a soft-deleted one Restore and Delete permanently (D34). */
function ProjectActions({ org, project }: { org: AdminOrgDetail; project: AdminOrgProject }) {
  const queryClient = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const [purging, setPurging] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const remove = useMutation({
    mutationFn: () => adminApi.deleteProject(org.slug, project.slug, project.name),
    onSuccess: async () => {
      setDeleting(false);
      await done();
    },
  });
  const purge = useMutation({
    mutationFn: () => adminApi.purgeProject(org.slug, project.slug, project.name),
    onSuccess: async () => {
      setPurging(false);
      await done();
    },
  });
  const restore = useMutation({
    mutationFn: () => adminApi.restoreProject(org.slug, project.slug),
    onSuccess: done,
    onError: toastError,
  });
  const orgGone = !!org.deleted_at;
  const restoreButton = (
    <Button size="sm" variant="secondary" disabled={orgGone || restore.isPending} onClick={() => restore.mutate()}>
      Restore
    </Button>
  );
  return (
    <span className="flex flex-wrap items-center gap-2">
      {project.deleted_at ? (
        <>
          {orgGone ? <DisabledReason reason="Restore the organization first.">{restoreButton}</DisabledReason> : restoreButton}
          <Button
            size="sm"
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              purge.reset();
              setPurging(true);
            }}
          >
            Delete permanently
          </Button>
        </>
      ) : (
        !orgGone && (
          <Button
            size="sm"
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              remove.reset();
              setDeleting(true);
            }}
          >
            Delete
          </Button>
        )
      )}
      <TypedConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        testId="delete-project-dialog"
        title={`Delete ${project.name}?`}
        description={`${project.name} disappears for everyone in ${org.name} at once, and its scans stop. Nothing is erased: you can restore it, or delete it permanently later.`}
        expected={project.name}
        confirmLabel="Delete project"
        pendingLabel="Deleting..."
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
      <TypedConfirmDialog
        open={purging}
        onOpenChange={setPurging}
        testId="purge-project-dialog"
        title={`Delete ${project.name} permanently?`}
        description="Its server copy and its history (descriptions, rationale cards, chats) are deleted. This cannot be undone. The repository on GitHub is not changed."
        expected={project.name}
        confirmLabel="Delete permanently"
        pendingLabel="Deleting..."
        pending={purge.isPending}
        error={purge.isError ? errorMessage(purge.error) : null}
        onConfirm={() => purge.mutate()}
      />
    </span>
  );
}

function Projects({ org }: { org: AdminOrgDetail }) {
  const columns: Column<AdminOrgProject>[] = [
    {
      key: "project",
      header: "Project",
      primary: true,
      cell: (p) => (
        <span className="flex min-w-0 flex-col leading-tight">
          <span className="truncate font-medium">{p.name}</span>
          <span className="truncate font-mono text-xs text-muted-foreground">{p.slug}</span>
        </span>
      ),
    },
    {
      key: "state",
      header: "State",
      cell: (p) => {
        const s = projectState(p);
        return <Badge variant={s.variant}>{s.label}</Badge>;
      },
    },
    { key: "scan", header: "Last scan", cell: (p) => timeAgo(p.last_scan_at) ?? "-" },
    { key: "actions", header: <span className="sr-only">Actions</span>, cell: (p) => <ProjectActions org={org} project={p} /> },
  ];
  return (
    <ResponsiveTable
      columns={columns}
      rows={org.projects}
      rowKey={(p) => p.slug}
      rowTestId={(p) => `org-project-${p.slug}`}
      empty={<p className="text-sm text-muted-foreground">This organization has no projects.</p>}
    />
  );
}

function Usage({ org }: { org: AdminOrgDetail }) {
  return (
    <div className="flex flex-col gap-4" data-testid="org-usage">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Spend this month" value={formatUsd(org.spend_mtd)} />
      </div>
      <ResponsiveTable
        columns={[
          {
            key: "scope",
            header: "Scope",
            primary: true,
            cell: (b) => (
              <span className="capitalize">
                {b.scope}
                {b.project ? `: ${b.project}` : b.member ? `: ${b.member}` : ""}
              </span>
            ),
          },
          { key: "limit", header: "Monthly limit", align: "right", cell: (b) => formatUsd(b.monthly_usd) },
          { key: "stop", header: "Hard stop", cell: (b) => (b.hard_stop ? "Yes" : "No") },
          { key: "spent", header: "Spent", align: "right", cell: (b) => formatUsd(b.spent_usd) },
        ]}
        rows={org.budgets}
        rowKey={(b) => `${b.scope}:${b.project ?? ""}:${b.member ?? ""}`}
        empty={<p className="text-sm text-muted-foreground">No budgets are set.</p>}
      />
    </div>
  );
}

function Detail({ org }: { org: AdminOrgDetail }) {
  const search = useSearch({ strict: false }) as AdminOrgSearch;
  const navigate = useNavigate();
  const me = useQuery({ queryKey: [...ADMIN, "me"], queryFn: adminApi.me });
  const tab = search.tab ?? "overview";
  const go = (next: AdminOrgSearch) =>
    void navigate({ to: "/admin/orgs/$slug", params: { slug: org.slug }, search: next, replace: true });
  const writable = !org.deleted_at;
  const tabs = ADMIN_ORG_TABS.map((t) => ({ value: t, label: TAB_LABELS[t] }));
  const filters: AuditUrlFilters = { event: search.event, actor: search.actor, from: search.from, to: search.to };
  return (
    <PageContainer width="default" className="flex flex-col gap-5" data-testid="admin-org">
      <div className="flex flex-col gap-1">
        <AdminHeading>{org.name}</AdminHeading>
        <p className="font-mono text-[13px] text-muted-foreground">{org.slug}</p>
      </div>
      {org.deleted_at ? (
        <DeletedAlert deletedAt={org.deleted_at} deletedBy={org.deleted_by} />
      ) : (
        org.suspended && (
          <Alert variant="warning" data-testid="suspended-alert">
            <AlertDescription>
              Suspended {formatDate(org.suspended.at)}: {org.suspended.reason}
            </AlertDescription>
          </Alert>
        )
      )}
      <OrgActions org={org} me={me.data} />
      <Tabs value={tab} onValueChange={(v) => go({ ...search, tab: v as AdminOrgTab })} className="gap-5">
        <TabsSelect label="Organization section" value={tab} onValueChange={(v) => go({ ...search, tab: v as AdminOrgTab })} tabs={tabs} />
        <TabsList variant="scrollable" className="max-sm:hidden" aria-label="Organization section">
          {tabs.map((t) => (
            <TabsTrigger key={t.value} value={t.value} className="flex-none text-[13px]">
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">{tab === "overview" && <Overview org={org} />}</TabsContent>
        <TabsContent value="members">{tab === "members" && <Members org={org} writable={writable} />}</TabsContent>
        <TabsContent value="projects">{tab === "projects" && <Projects org={org} />}</TabsContent>
        <TabsContent value="usage">{tab === "usage" && <Usage org={org} />}</TabsContent>
        <TabsContent value="audit">
          {tab === "audit" && (
            <AuditTable
              scope="admin"
              org={org.slug}
              filename="whygraph-admin-audit.csv"
              filters={filters}
              onFilters={(next) => go({ tab: "audit", event: next.event, actor: next.actor, from: next.from, to: next.to })}
            />
          )}
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}

/** `/admin/orgs/$slug`: one organization with Overview, Members, Projects, Usage and Audit. */
export function AdminOrgPage() {
  const { slug } = useParams({ strict: false }) as { slug: string };
  const org = useQuery({ queryKey: [...ADMIN, "org", slug], queryFn: () => adminApi.org(slug), retry: false });
  if (org.data === undefined) {
    if (hasStatus(org.error, 404)) return <AdminNotFound label="Back to organizations" to="/admin/orgs" />;
    if (org.isError) {
      return (
        <PageContainer width="narrow">
          <ErrorState error={org.error} title="Couldn't load the organization" onRetry={() => void org.refetch()} />
        </PageContainer>
      );
    }
    return (
      <PageContainer width="default">
        <FormSkeleton />
      </PageContainer>
    );
  }
  return <Detail org={org.data} />;
}
