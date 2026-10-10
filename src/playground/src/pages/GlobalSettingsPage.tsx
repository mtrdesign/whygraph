import { useState } from "react";
import { toast } from "sonner";
import { Link, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { membersApi, orgSettingsApi, orgsApi, portalApi, portalKey, type Member } from "../api";
import { ConfigForm, configSections } from "../components/portal/ConfigForm";
import { Field, nativeSelectClass } from "../components/portal/Field";
import { TypedConfirmDialog } from "../components/portal/TypedConfirmDialog";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PathText } from "../components/layout/PathText";
import { ReadOnlyNotice } from "../components/settings/ReadOnlyNotice";
import { SectionForm } from "../components/settings/SectionForm";
import { SettingsLayout, SettingsSection, type SettingsNavItem } from "../components/settings/SettingsLayout";
import { authMessage } from "../lib/authErrors";
import { providerLabel } from "../lib/labels";
import type { OrgSettingsSearch } from "../lib/routeSearch";
import { canOwn, isProduction, usePortalState, useReadOnly, useRole } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

type Cleared = { slug: string; provider: string }[];

/**
 * "Delete organization" (production, owners only; M2d-2 plan section 4.8):
 * immediate and final, confirmed by typing the org's slug. Running scans are
 * cancelled; a sync still fetching makes the server answer `409 busy`, and the
 * owner tries again. Afterwards the owner lands on the base host's org picker.
 */
function DeleteOrganization({ slug, name, baseUrl }: { slug: string; name: string; baseUrl: string }) {
  const [open, setOpen] = useState(false);
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const count = projects.data?.projects.length;
  const remove = useMutation({
    mutationFn: () => orgsApi.remove(slug),
    onSuccess: () => void hardNavigate(`${new URL(baseUrl).origin}/orgs`),
  });

  return (
    <SettingsSection
      id="danger"
      title="Danger zone"
      danger
      testId="org-danger-zone"
      description={
        <>
          Deletes this organization with its projects, scans, members' access, settings and keys. There is no
          undo, and the name <span className="font-mono">{slug}</span> cannot be used again.
        </>
      }
    >
      <div>
        <Button
          variant="outline"
          className="text-destructive"
          onClick={() => {
            remove.reset();
            setOpen(true);
          }}
        >
          Delete organization
        </Button>
      </div>
      <TypedConfirmDialog
        open={open}
        onOpenChange={setOpen}
        testId="delete-org-dialog"
        errorTestId="delete-org-error"
        title={`Delete ${name}?`}
        description={
          <>
            {count === undefined
              ? "Every project of this organization is deleted with it."
              : count === 1
                ? "Its 1 project is deleted with it."
                : `Its ${count} projects are deleted with it.`}{" "}
            Running scans are cancelled. The repositories on GitHub are not changed.
          </>
        }
        expected={slug}
        confirmLabel="Delete organization"
        pendingLabel="Deleting…"
        pending={remove.isPending}
        error={remove.isError ? authMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
    </SettingsSection>
  );
}

const DEFAULT_ROLE_OPTIONS: { value: "contributor" | "viewer" | "none"; label: string }[] = [
  { value: "contributor", label: "Contributor - read, chat and quick rescans" },
  { value: "viewer", label: "Viewer - read only" },
  { value: "none", label: "None - only people given access see a project" },
];

/** General (production, owners): the organization's name and the default project role, as a section form. */
function OrgGeneral({ name, defaultRole }: { name: string; defaultRole: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(name);
  const [role, setRole] = useState(defaultRole);
  const dirty = draft.trim() !== name || role !== defaultRole;
  return (
    <SectionForm
      name="General"
      testId="org-general"
      dirty={dirty}
      saveDisabled={!draft.trim()}
      onSave={async () => {
        await orgSettingsApi.patch({
          ...(draft.trim() !== name ? { name: draft.trim() } : {}),
          ...(role !== defaultRole ? { default_project_role: role as "contributor" | "viewer" | "none" } : {}),
        });
        await queryClient.invalidateQueries({ queryKey: portalKey("state") });
      }}
      onDiscard={() => {
        setDraft(name);
        setRole(defaultRole);
      }}
    >
      <Field label="Organization name">
        {(p) => <Input {...p} value={draft} onChange={(e) => setDraft(e.target.value)} />}
      </Field>
      <Field
        label="Default project role"
        hint="What a member gets on a project they have no grant on. A project marked Restricted ignores it."
      >
        {(p) => (
          <select {...p} className={nativeSelectClass} value={role} onChange={(e) => setRole(e.target.value)}>
            {DEFAULT_ROLE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
      </Field>
    </SectionForm>
  );
}

/** Ownership (production, owners): hand the organization to another member. */
function Ownership({ slug, name, me }: { slug: string; name: string; me: string | undefined }) {
  const queryClient = useQueryClient();
  const members = useQuery({ queryKey: portalKey("members"), queryFn: membersApi.list });
  const candidates = (members.data ?? []).filter((m) => m.uid !== me && m.role !== "owner" && !m.disabled);
  const [target, setTarget] = useState("");
  const [open, setOpen] = useState(false);
  const transfer = useMutation({
    mutationFn: () => orgSettingsApi.transfer(target, slug),
    onSuccess: async () => {
      setOpen(false);
      setTarget("");
      toast.success("Ownership transferred. You are now an admin.");
      await queryClient.invalidateQueries({ queryKey: portalKey("state") });
      // The defaults payload's `read_only` follows the new role.
      await queryClient.invalidateQueries({ queryKey: portalKey("defaults") });
      await queryClient.invalidateQueries({ queryKey: portalKey("members") });
    },
  });
  const chosen = candidates.find((m) => m.uid === target);
  const label = (m: Member) => m.display_name || (m.github_login ? `@${m.github_login}` : m.uid);
  return (
    <div className="flex flex-col gap-4 border-t border-border pt-4" data-testid="org-ownership">
      <div>
        <h3 className="text-[13px] font-medium">Ownership</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Make another member the owner of {name}. You become an admin. To share ownership instead, give
          someone the owner role on the Members page.
        </p>
      </div>
      <Field label="New owner">
        {(p) => (
          <select {...p} className={nativeSelectClass} value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Choose a member</option>
            {candidates.map((m) => (
              <option key={m.uid} value={m.uid}>
                {label(m)}
              </option>
            ))}
          </select>
        )}
      </Field>
      <div>
        <Button
          variant="outline"
          disabled={!target}
          onClick={() => {
            transfer.reset();
            setOpen(true);
          }}
        >
          Transfer ownership
        </Button>
      </div>
      <TypedConfirmDialog
        open={open}
        onOpenChange={setOpen}
        testId="transfer-dialog"
        errorTestId="transfer-error"
        title={`Transfer ${name} to ${chosen ? label(chosen) : "this member"}?`}
        description="They become the owner and you become an admin. Only the new owner can give ownership back."
        expected={slug}
        confirmLabel="Transfer ownership"
        pendingLabel="Transferring…"
        pending={transfer.isPending}
        error={transfer.isError ? authMessage(transfer.error) : null}
        onConfirm={() => transfer.mutate()}
      />
    </div>
  );
}

/** General for everyone but an editing owner: what the org or the portal is. */
function GeneralInfo() {
  const state = usePortalState().data;
  const org = state?.org;
  if (isProduction(state)) {
    const roleText = DEFAULT_ROLE_OPTIONS.find((o) => o.value === (org?.default_project_role ?? "contributor"))?.label;
    return (
      <dl className="flex flex-col gap-2 text-sm" data-testid="org-general-read">
        <InfoRow label="Name">{org?.name ?? "-"}</InfoRow>
        <InfoRow label="Default project role">{roleText ?? "-"}</InfoRow>
      </dl>
    );
  }
  return (
    <dl className="flex flex-col gap-2 text-sm" data-testid="portal-general">
      {state?.version && <InfoRow label="Version">{state.version}</InfoRow>}
      {state?.port && <InfoRow label="Port">{state.port}</InfoRow>}
      <InfoRow label="Shared folders">
        {state?.shared_folders?.length ? (
          <ul className="flex flex-col gap-0.5">
            {state.shared_folders.map((f) => (
              <li key={f}>
                <PathText path={f} variant="block" />
              </li>
            ))}
          </ul>
        ) : (
          "None yet"
        )}
      </InfoRow>
    </dl>
  );
}

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:gap-4">
      <dt className="shrink-0 text-muted-foreground sm:w-40">{label}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  );
}

/**
 * Screen 11: the defaults every project inherits (models, provider keys,
 * endpoints, the GitHub token locally, the org agent limits in production),
 * with a section list (`?section=`) like project settings. Changing an endpoint
 * clears the keys that were tied to the old one; after a save this page lists
 * the projects whose own key was cleared that way, because they must enter it
 * again.
 *
 * Only an owner may change them (`org.configure`; the payload's `read_only`);
 * everyone else sees them read-only with a notice. In production an owner also
 * gets the organization's name, ownership and the Delete organization danger zone.
 */
export function GlobalSettingsPage() {
  const search = useSearch({ strict: false }) as OrgSettingsSearch;
  const [cleared, setCleared] = useState<Cleared>([]);
  const role = useRole();
  const reader = useReadOnly();
  const defaults = useQuery({ queryKey: portalKey("defaults"), queryFn: portalApi.defaults });
  const state = usePortalState().data;
  const production = isProduction(state);
  const org = state?.org;
  const readOnly = reader || (defaults.data?.read_only ?? !canOwn(role));
  const editable = !readOnly;
  const ownerTools = production && editable && !!org;

  const sections: SettingsNavItem[] = [
    { id: "general", label: "General" },
    ...configSections("global", { production, configurer: editable }),
    { id: "budgets", label: "Budgets" },
    ...(ownerTools && state?.base_url ? [{ id: "danger", label: "Danger zone" }] : []),
  ];

  const notices = (
    <>
      {reader ? (
        <ReadOnlyNotice audience={{ kind: "reader" }} />
      ) : (
        readOnly && defaults.data && <ReadOnlyNotice audience={{ kind: "org" }} />
      )}
      {cleared.length > 0 && (
        <Alert variant="warning" data-testid="cleared-keys">
          <AlertTitle>Some project keys were cleared</AlertTitle>
          <AlertDescription>
            <p>
              The endpoint changed, so these projects' own keys for it were removed (a key must not follow a
              changed endpoint). Enter them again in each project's settings:
            </p>
            <ul className="mt-1 list-disc pl-4">
              {cleared.map((c) => (
                <li key={`${c.slug}:${c.provider}`}>
                  <Link
                    to="/p/$slug/settings"
                    params={{ slug: c.slug }}
                    search={{ section: "models" }}
                    className="text-primary-text hover:underline"
                  >
                    {c.slug}
                  </Link>{" "}
                  - {providerLabel(c.provider)}
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
    </>
  );

  return (
    <SettingsLayout
      title="Settings"
      description="Defaults for every project. A project can override any of these in its own settings."
      sections={sections}
      initial={search.section}
      notices={notices}
    >
      <SettingsSection id="general" title="General">
        {ownerTools && org ? (
          <>
            <OrgGeneral
              key={`${org.name}|${org.default_project_role ?? ""}`}
              name={org.name}
              defaultRole={org.default_project_role ?? "contributor"}
            />
            <Ownership slug={org.slug} name={org.name} me={state?.user?.uid} />
          </>
        ) : (
          <GeneralInfo />
        )}
      </SettingsSection>
      <ConfigForm
        scope={{ kind: "global" }}
        readOnly={readOnly}
        onSaved={(saved) => setCleared(saved?.cleared_project_keys ?? [])}
      />
      <SettingsSection
        id="budgets"
        title="Budgets"
        description={
          production
            ? "Monthly budgets for the organization, its members and its projects live on Usage & cost."
            : "Monthly budgets for the portal and its projects live on Usage & cost."
        }
      >
        <div>
          <Button variant="outline" size="sm" render={<Link to="/usage" search={{ tab: "budgets" }} />}>
            Open budgets
          </Button>
        </div>
      </SettingsSection>
      {ownerTools && org && state?.base_url && (
        <DeleteOrganization slug={org.slug} name={org.name} baseUrl={state.base_url} />
      )}
    </SettingsLayout>
  );
}
