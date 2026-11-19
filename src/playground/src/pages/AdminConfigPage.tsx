import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserCheckIcon } from "lucide-react";
import {
  adminApi,
  ApiError,
  portalKey,
  type AdminConfigChanges,
  type AdminInstanceSettings,
  type AllowlistEntry,
  type SignupPolicy,
} from "../api";
import { ADMIN, toastError, useUserName } from "../components/admin/adminParts";
import { InstanceCard } from "../components/admin/InstanceCard";
import { AdminHeading } from "../components/admin/AdminHeading";
import { PageContainer } from "../components/layout/PageContainer";
import { ResponsiveTable, type Column } from "../components/layout/ResponsiveTable";
import { ConfirmDialog } from "../components/portal/ConfirmDialog";
import { Field, nativeSelectClass } from "../components/portal/Field";
import { SectionForm } from "../components/settings/SectionForm";
import { SettingsLayout, SettingsSection } from "../components/settings/SettingsLayout";
import { AnnouncementStrip } from "../components/shell/AnnouncementBanner";
import { EmptyState } from "../components/state/EmptyState";
import { QueryState } from "../components/state/QueryState";
import { Skeleton } from "../components/ui/skeleton";
import { TableSkeleton } from "../components/state/Skeletons";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { errorMessage } from "../lib/apiErrors";
import { formatDate, formatDateTime } from "../lib/format";

type Save = (changes: AdminConfigChanges) => Promise<unknown>;

const SIGNUP_OPTIONS: { value: SignupPolicy; label: string; hint: string }[] = [
  {
    value: "open",
    label: "Anyone with a GitHub account",
    hint: "Anyone who signs in with GitHub gets an account.",
  },
  {
    value: "invite_only",
    label: "Invited people and the allowlist",
    hint: "A new account needs an open invitation from an organization, or an entry on the allowlist below. People who already have an account are not affected.",
  },
  {
    value: "closed",
    label: "Nobody new",
    hint: "Only people who already have an account can sign in. Inviting someone without an account is refused.",
  },
];

const ORG_CREATION_OPTIONS = [
  { value: "everyone", label: "Everyone" },
  { value: "admins", label: "Platform administrators only" },
] as const;

const MAX_QUOTA = 2147483647;
const QUOTA_ERROR = "Enter a whole number of 1 or more, or leave it empty.";
const BANNER_LIMIT = 500;

/** A quota field's text as the value to send: `null` for empty, `undefined` when it is not a whole number of 1 or more. */
function parseQuota(text: string): number | null | undefined {
  const t = text.trim();
  if (t === "") return null;
  if (!/^\d+$/.test(t)) return undefined;
  const n = Number(t);
  return n >= 1 && n <= MAX_QUOTA ? n : undefined;
}

const quotaText = (n: number | null) => (n === null ? "" : String(n));

function SignupSection({ settings, save }: { settings: AdminInstanceSettings; save: Save }) {
  const [policy, setPolicy] = useState<SignupPolicy>(settings.signup_policy);
  const chosen = SIGNUP_OPTIONS.find((o) => o.value === policy);
  return (
    <SettingsSection id="signup" title="Sign-up" testId="config-signup">
      <SectionForm
        name="Sign-up"
        testId="config-signup-form"
        dirty={policy !== settings.signup_policy}
        onSave={() => save({ signup_policy: policy })}
        onDiscard={() => setPolicy(settings.signup_policy)}
      >
        <Field label="Who can sign up" hint={chosen?.hint}>
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={policy}
              onChange={(e) => setPolicy(e.target.value as SignupPolicy)}
            >
              {SIGNUP_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          )}
        </Field>
      </SectionForm>
    </SettingsSection>
  );
}

function OrganizationsSection({ settings, save }: { settings: AdminInstanceSettings; save: Save }) {
  const [creation, setCreation] = useState(settings.org_creation);
  return (
    <SettingsSection id="organizations" title="Organizations" testId="config-organizations">
      <SectionForm
        name="Organizations"
        testId="config-organizations-form"
        dirty={creation !== settings.org_creation}
        onSave={() => save({ org_creation: creation })}
        onDiscard={() => setCreation(settings.org_creation)}
      >
        <Field
          label="Who can create organizations"
          hint="Platform administrators can always create an organization in the admin panel."
        >
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={creation}
              onChange={(e) => setCreation(e.target.value as "everyone" | "admins")}
            >
              {ORG_CREATION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          )}
        </Field>
      </SectionForm>
    </SettingsSection>
  );
}

function LimitsSection({ settings, save }: { settings: AdminInstanceSettings; save: Save }) {
  const [orgs, setOrgs] = useState(quotaText(settings.max_orgs_per_user));
  const [projects, setProjects] = useState(quotaText(settings.max_projects_per_org));
  const [errors, setErrors] = useState<{ orgs?: string; projects?: string }>({});
  const dirty = orgs !== quotaText(settings.max_orgs_per_user) || projects !== quotaText(settings.max_projects_per_org);
  return (
    <SettingsSection id="limits" title="Limits" testId="config-limits">
      <SectionForm
        name="Limits"
        testId="config-limits-form"
        dirty={dirty}
        onSave={async () => {
          const o = parseQuota(orgs);
          const p = parseQuota(projects);
          const next = {
            ...(o === undefined ? { orgs: QUOTA_ERROR } : {}),
            ...(p === undefined ? { projects: QUOTA_ERROR } : {}),
          };
          setErrors(next);
          // A bad number is refused here: nothing is sent.
          if (o === undefined || p === undefined) return false;
          const changes: AdminConfigChanges = {};
          if (o !== settings.max_orgs_per_user) changes.max_orgs_per_user = o;
          if (p !== settings.max_projects_per_org) changes.max_projects_per_org = p;
          await save(changes);
        }}
        onDiscard={() => {
          setOrgs(quotaText(settings.max_orgs_per_user));
          setProjects(quotaText(settings.max_projects_per_org));
          setErrors({});
        }}
      >
        <Field label="Organizations one person may own" hint="Leave empty for no limit." error={errors.orgs}>
          {(p) => (
            <Input
              {...p}
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={orgs}
              onChange={(e) => {
                setOrgs(e.target.value);
                setErrors((prev) => ({ ...prev, orgs: undefined }));
              }}
            />
          )}
        </Field>
        <Field label="Projects per organization" hint="Leave empty for no limit." error={errors.projects}>
          {(p) => (
            <Input
              {...p}
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={projects}
              onChange={(e) => {
                setProjects(e.target.value);
                setErrors((prev) => ({ ...prev, projects: undefined }));
              }}
            />
          )}
        </Field>
      </SectionForm>
    </SettingsSection>
  );
}

function AnnouncementSection({ settings, save }: { settings: AdminInstanceSettings; save: Save }) {
  const [text, setText] = useState(settings.banner_text ?? "");
  const [level, setLevel] = useState(settings.banner_level);
  const dirty = text !== (settings.banner_text ?? "") || level !== settings.banner_level;
  return (
    <SettingsSection
      id="announcement"
      title="Announcement"
      description="A banner shown at the top of every page. Plain text only."
      testId="config-announcement"
    >
      <SectionForm
        name="Announcement"
        testId="config-announcement-form"
        dirty={dirty}
        saveDisabled={text.length > BANNER_LIMIT}
        onSave={() => {
          const changes: AdminConfigChanges = {};
          if (text !== (settings.banner_text ?? "")) changes.banner_text = text.trim() === "" ? null : text;
          if (level !== settings.banner_level) changes.banner_level = level;
          return save(changes);
        }}
        onDiscard={() => {
          setText(settings.banner_text ?? "");
          setLevel(settings.banner_level);
        }}
      >
        <Field label="Banner text" hint="Everyone sees this text, signed in or not. Leave it empty to show no banner.">
          {(p) => (
            <>
              <Textarea {...p} maxLength={BANNER_LIMIT} value={text} onChange={(e) => setText(e.target.value)} />
              <p
                className="text-right text-xs text-muted-foreground"
                data-testid="banner-count"
                aria-live="polite"
              >
                {text.length} / {BANNER_LIMIT}
              </p>
            </>
          )}
        </Field>
        <Field label="Banner style">
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={level}
              onChange={(e) => setLevel(e.target.value as "info" | "warning")}
            >
              <option value="info">Info</option>
              <option value="warning">Warning</option>
            </select>
          )}
        </Field>
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">Preview</span>
          {text.trim() ? (
            <div className="overflow-hidden rounded-lg border border-border">
              <AnnouncementStrip text={text} level={level} testId="announcement-preview" />
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No banner is shown while the text is empty.</p>
          )}
        </div>
      </SectionForm>
    </SettingsSection>
  );
}

function AllowlistSection() {
  const queryClient = useQueryClient();
  const userName = useUserName();
  const list = useQuery({ queryKey: [...ADMIN, "allowlist"], queryFn: adminApi.allowlist });
  const [login, setLogin] = useState("");
  const [note, setNote] = useState("");
  const [existing, setExisting] = useState<string | null>(null);
  const [removing, setRemoving] = useState<AllowlistEntry | null>(null);

  const add = useMutation({
    mutationFn: () =>
      adminApi.addToAllowlist({
        github_login: login.trim().replace(/^@/, ""),
        ...(note.trim() ? { note: note.trim() } : {}),
      }),
    onSuccess: async (entry) => {
      setExisting(entry.added ? null : entry.github_login);
      setLogin("");
      setNote("");
      await queryClient.invalidateQueries({ queryKey: ADMIN });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => adminApi.removeFromAllowlist(id),
    onSuccess: async () => {
      setRemoving(null);
      await queryClient.invalidateQueries({ queryKey: ADMIN });
    },
    onError: (err) => {
      // The entry may already be gone: close the dialog and let the list say so.
      setRemoving(null);
      toastError(err);
    },
  });

  const code = add.error instanceof ApiError ? add.error.code : undefined;
  const fieldError =
    code === "no_such_github_user" || code === "bad_login" ? errorMessage(add.error) : undefined;
  const alertError = add.isError && !fieldError ? errorMessage(add.error) : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!login.trim() || add.isPending) return;
    setExisting(null);
    add.mutate();
  };

  const columns: Column<AllowlistEntry>[] = [
    {
      key: "login",
      header: "GitHub account",
      primary: true,
      cell: (e) => <span className="break-all font-mono text-[13px]">@{e.github_login}</span>,
    },
    { key: "note", header: "Note", cell: (e) => e.note ?? <span className="text-muted-foreground">-</span> },
    {
      key: "added",
      header: "Added",
      cell: (e) => `${formatDate(e.created_at)} by ${userName(e.created_by)}`,
    },
    {
      key: "used",
      header: "Used",
      cell: (e) => (e.used_at ? formatDate(e.used_at) : <span className="text-muted-foreground">Not used yet</span>),
    },
    {
      key: "remove",
      header: <span className="sr-only">Remove</span>,
      align: "right",
      cell: (e) => (
        <Button
          size="sm"
          variant="outline"
          aria-label={`Remove @${e.github_login}`}
          onClick={() => {
            remove.reset();
            setRemoving(e);
          }}
        >
          Remove
        </Button>
      ),
    },
  ];

  return (
    <SettingsSection
      id="allowlist"
      title="Sign-up allowlist"
      description="Each entry lets one GitHub account sign up, once. Renaming the account on GitHub changes nothing."
      testId="config-allowlist"
    >
      <form noValidate className="flex flex-col gap-3" onSubmit={submit} data-testid="allowlist-form">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="GitHub username" error={fieldError}>
            {(p) => (
              <Input
                {...p}
                autoComplete="off"
                spellCheck={false}
                value={login}
                onChange={(e) => {
                  setLogin(e.target.value);
                  add.reset();
                  setExisting(null);
                }}
              />
            )}
          </Field>
          <Field label="Note (optional, up to 200 characters)">
            {(p) => <Input {...p} maxLength={200} value={note} onChange={(e) => setNote(e.target.value)} />}
          </Field>
        </div>
        {alertError && (
          <Alert variant="destructive" data-testid="allowlist-error">
            <AlertDescription>{alertError}</AlertDescription>
          </Alert>
        )}
        {existing && (
          <Alert variant="info" data-testid="allowlist-existing">
            <AlertDescription>@{existing} is already on the list.</AlertDescription>
          </Alert>
        )}
        <div>
          <Button type="submit" disabled={!login.trim() || add.isPending}>
            {add.isPending ? "Adding…" : "Add"}
          </Button>
        </div>
      </form>
      <QueryState query={list} loading={<TableSkeleton />} errorTitle="Couldn't load the allowlist">
        {(entries) => (
          <ResponsiveTable
            columns={columns}
            rows={entries}
            rowKey={(e) => String(e.id)}
            rowTestId={(e) => `allowlist-${e.id}`}
            empty={
              <EmptyState
                icon={<UserCheckIcon />}
                title="Nobody is on the allowlist"
                description="Add a GitHub username to let that account sign up."
              />
            }
          />
        )}
      </QueryState>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={`Remove @${removing?.github_login ?? ""} from the allowlist?`}
        description="They can no longer sign up unless an organization invites them."
        confirmLabel="Remove"
        pending={remove.isPending}
        error={null}
        onConfirm={() => removing && remove.mutate(removing.id)}
      />
    </SettingsSection>
  );
}

/** `/admin/config`: platform-wide policy, the banner, the sign-up allowlist and the instance's facts. */
export function AdminConfigPage() {
  const queryClient = useQueryClient();
  const userName = useUserName();
  const config = useQuery({ queryKey: [...ADMIN, "config"], queryFn: adminApi.config });

  // Each form sends only its own changed keys; a refetch then moves every form's saved values.
  const save: Save = async (changes) => {
    if (Object.keys(changes).length === 0) return;
    await adminApi.saveConfig(changes);
    // The banner and the quota-aware pages read the portal state too.
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ADMIN }),
      queryClient.invalidateQueries({ queryKey: portalKey("state") }),
    ]);
  };

  return (
    <QueryState
      query={config}
      errorTitle="Couldn't load the platform configuration"
      loading={
        <PageContainer className="flex flex-col gap-4">
          <AdminHeading>Platform configuration</AdminHeading>
          <Skeleton className="h-40" />
        </PageContainer>
      }
    >
      {({ settings, instance }) => (
        <SettingsLayout
          title="Platform configuration"
          description={
            settings.updated_at ? (
              <span data-testid="config-changed">
                Last changed {formatDateTime(settings.updated_at)}
                {settings.updated_by ? ` by ${userName(settings.updated_by)}` : ""}
              </span>
            ) : (
              "Not changed yet: the defaults apply."
            )
          }
          sections={[
            { id: "signup", label: "Sign-up" },
            { id: "organizations", label: "Organizations" },
            { id: "limits", label: "Limits" },
            { id: "announcement", label: "Announcement" },
            ...(settings.signup_policy === "invite_only" ? [{ id: "allowlist", label: "Allowlist" }] : []),
            { id: "instance", label: "Instance" },
          ]}
        >
          <SignupSection settings={settings} save={save} />
          <OrganizationsSection settings={settings} save={save} />
          <LimitsSection settings={settings} save={save} />
          <AnnouncementSection settings={settings} save={save} />
          {settings.signup_policy === "invite_only" && <AllowlistSection />}
          <SettingsSection id="instance" title="Instance" testId="config-instance">
            <InstanceCard instance={instance} />
          </SettingsSection>
        </SettingsLayout>
      )}
    </QueryState>
  );
}
