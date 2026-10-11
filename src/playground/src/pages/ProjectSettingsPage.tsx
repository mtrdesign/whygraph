import { useState } from "react";
import { useSearch } from "@tanstack/react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { portalApi, portalKey, projectApi, projectKey, type ProjectDetails } from "../api";
import { canAdmin, isProduction, usePortalState, useReadOnly, useRole } from "../lib/identity";
import { agentLabel, sourceLabel } from "../lib/labels";
import { can } from "../lib/permissions";
import { useSlug } from "../lib/project";
import type { ProjectSettingsSearch } from "../lib/routeSearch";
import { LinkNotice } from "../components/portal/LinkNotice";
import { PlatformButtons } from "../components/portal/LinkedActions";
import { LinkedHooks } from "../components/portal/LinkedHooks";
import { ConfigForm, configSections } from "../components/portal/ConfigForm";
import { ProjectConnectedPortals } from "../components/portal/ConnectedPortals";
import { CopyButton } from "../components/portal/CopyButton";
import { ImportingNotice, NotInitialized, ProjectUnavailable } from "../components/portal/EdgeStates";
import { Field } from "../components/portal/Field";
import { InitializeStep } from "../components/portal/InitializeStep";
import { ProjectUsageSection } from "../components/usage/ProjectUsageSection";
import { ProjectAccess } from "../components/portal/ProjectAccess";
import { ProjectPortChangeNotice } from "../components/portal/PortChangeNotice";
import { RemoveProjectDialog } from "../components/portal/RemoveProjectDialog";
import { PathText } from "../components/layout/PathText";
import { ReadOnlyNotice } from "../components/settings/ReadOnlyNotice";
import { SectionForm } from "../components/settings/SectionForm";
import { SettingsLayout, SettingsSection, type SettingsNavItem } from "../components/settings/SettingsLayout";
import { PageSkeleton } from "../components/state/Skeletons";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";

/** A definition list row that wraps on a phone (label above value). */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:gap-4">
      <dt className="shrink-0 text-muted-foreground sm:w-28">{label}</dt>
      <dd className="min-w-0 flex-1">{children}</dd>
    </div>
  );
}

function McpRow({ url }: { url: string }) {
  return (
    <Row label="MCP endpoint">
      <span className="row-wrap gap-2">
        <span className="font-mono text-xs break-all">{url}</span>
        <CopyButton text={url} variant="ghost" />
      </span>
    </Row>
  );
}

/** General: the display name (a section form) and where the project comes from. */
function General({
  project: p,
  production,
  readOnly,
  shared,
}: {
  project: ProjectDetails;
  production: boolean;
  readOnly: boolean;
  shared: string[] | undefined;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState<string | null>(null);

  if (p.source === "platform") {
    return (
      <SettingsSection id="general" title="General">
        <div className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">Display name</span>
          <span className="text-sm" data-testid="linked-name">
            {p.name}
          </span>
          <p className="text-xs text-muted-foreground">The name follows the project on the platform; change it there.</p>
        </div>
        <LinkNotice project={p} />
        <PlatformButtons project={p} />
        <dl className="flex flex-col gap-2 text-sm">
          <Row label="Source">Linked platform project</Row>
          {p.root && (
            <Row label="Folder">
              <PathText path={p.root} base={shared} variant="block" copy />
            </Row>
          )}
          {p.mcp_url && <McpRow url={p.mcp_url} />}
        </dl>
      </SettingsSection>
    );
  }

  const value = name ?? p.name;
  return (
    <SettingsSection id="general" title="General">
      <SectionForm
        name="General"
        dirty={value.trim() !== p.name}
        saveDisabled={value.trim() === ""}
        readOnly={readOnly}
        onSave={async () => {
          await projectApi(p.slug).rename(value.trim());
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: projectKey(p.slug, "project") }),
            queryClient.invalidateQueries({ queryKey: portalKey("projects") }),
          ]);
          setName(null);
        }}
        onDiscard={() => setName(null)}
      >
        <Field
          label="Display name"
          hint={
            <>
              The URL name <span className="font-mono">{p.slug}</span> never changes.
            </>
          }
        >
          {(f) => <Input {...f} value={value} onChange={(e) => setName(e.target.value)} />}
        </Field>
      </SectionForm>
      {production ? (
        <dl className="flex flex-col gap-2 text-sm" data-testid="github-repo">
          <Row label="Repository">
            <span className="font-mono text-xs break-all">
              {p.remote_url ? (
                <a href={p.remote_url} target="_blank" rel="noreferrer" className="hover:underline">
                  {p.github_full_name ?? p.remote_url}
                </a>
              ) : (
                (p.github_full_name ?? "-")
              )}
            </span>
          </Row>
          <Row label="Installation">
            <span className="font-mono text-xs">{p.installation_account ?? "-"}</span>
          </Row>
        </dl>
      ) : (
        <dl className="flex flex-col gap-2 text-sm">
          <Row label="Source">{p.source === "github" ? "GitHub clone" : sourceLabel(p.source)}</Row>
          {p.root && (
            <Row label="Folder">
              <PathText path={p.root} base={shared} variant="block" copy />
            </Row>
          )}
          {p.remote_url && (
            <Row label="Remote">
              <span className="font-mono text-xs break-all">{p.remote_url}</span>
            </Row>
          )}
          {p.mcp_url && <McpRow url={p.mcp_url} />}
        </dl>
      )}
    </SettingsSection>
  );
}

/** Agents: the picker in settings mode (a section form), or why it cannot be changed here. */
function Agents({ project: p, canSetup }: { project: ProjectDetails; canSetup: boolean }) {
  const queryClient = useQueryClient();
  let body: React.ReactNode;
  if (!p.initialized) body = <NotInitialized slug={p.slug} />;
  else if (p.root_status !== "ok")
    body = <p className="text-sm text-muted-foreground">The folder is not available, so its agent files can't be changed.</p>;
  else if (canSetup)
    body = (
      <InitializeStep
        slug={p.slug}
        mode="settings"
        configured={p.agents}
        detected={p.detected ?? null}
        onDone={() => void queryClient.invalidateQueries({ queryKey: projectKey(p.slug, "project") })}
      />
    );
  else
    body = (
      <p className="text-sm text-muted-foreground" data-testid="agents-read-only">
        {p.agents.length ? `Set up for ${p.agents.map(agentLabel).join(", ")}.` : "No agent is set up."}
      </p>
    );
  return (
    <SettingsSection
      id="agents"
      title="Agents"
      description="The coding agents connected to this project over HTTP. Tick one to add it, untick to remove its entry."
    >
      {body}
    </SettingsSection>
  );
}

/**
 * Project settings (screen 10, SET-1..SET-4, plan section 4.13): a section list
 * (`?section=` deep links) beside General, Models and keys, GitHub, Git hooks,
 * Usage, Agents, Access, Connected portals and the Danger zone, each saving on
 * its own. Read-only follows the config payload's `read_only` (never a role
 * name); a linked project shows only General (read-only), Agents and Git hooks,
 * because the platform owns the rest. An importing project keeps its settings
 * and says it is importing (never "folder missing" or "not set up").
 */
export function ProjectSettingsPage() {
  const slug = useSlug();
  const search = useSearch({ strict: false }) as ProjectSettingsSearch;
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const config = useQuery({ queryKey: projectKey(slug, "config"), queryFn: () => projectApi(slug).config() });
  const reader = useReadOnly();
  const state = usePortalState().data;
  const production = isProduction(state);
  const role = useRole();
  const [removing, setRemoving] = useState(false);
  const p = project.data;
  if (!p) return <PageSkeleton width="default" />;

  const linked = p.source === "platform";
  const configurer = can(p, "project.configure");
  // The payload decides (Q9); before it lands, the project's permissions do.
  const readOnly = reader || (linked ? true : (config.data?.read_only ?? !configurer));
  const showConnections = production && configurer;
  const showAccess = production && can(p, "project.access");
  const showUsage = can(p, "project.usage") && !linked;
  // Removing a project is an org action (ORG_REMOVE_PROJECT), not a project one.
  const showDanger = canAdmin(role) && !reader;
  const showAgents = !production;
  const canSetup = !reader && can(p, "project.setup");

  const sections: SettingsNavItem[] = linked
    ? [
        { id: "general", label: "General" },
        { id: "agents", label: "Agents" },
        { id: "hooks", label: "Git hooks" },
      ]
    : [
        { id: "general", label: "General" },
        ...configSections("project", { production, source: p.source }),
        ...(showUsage ? [{ id: "budgets", label: "Usage" }] : []),
        ...(showAgents ? [{ id: "agents", label: "Agents" }] : []),
        ...(showAccess ? [{ id: "access", label: "Access" }] : []),
        ...(showConnections ? [{ id: "connections", label: "Connected portals" }] : []),
      ];
  if (showDanger) sections.push({ id: "danger", label: "Danger zone" });

  const notices = (
    <>
      {linked ? (
        <ReadOnlyNotice audience={{ kind: "linked", platformOrigin: p.link?.platform_origin }} />
      ) : reader ? (
        <ReadOnlyNotice audience={{ kind: "reader" }} />
      ) : (
        readOnly && config.data && <ReadOnlyNotice audience={{ kind: "project" }} />
      )}
      {p.importing && <ImportingNotice project={p} />}
      {!p.importing && p.root_status !== "ok" && <ProjectUnavailable project={p} />}
      <ProjectPortChangeNotice slug={slug} change={p.port_change} />
    </>
  );

  return (
    <SettingsLayout title="Settings" sections={sections} initial={search.section} notices={notices}>
      <General project={p} production={production} readOnly={readOnly} shared={state?.shared_folders} />

      {linked ? (
        <>
          <Agents project={p} canSetup={canSetup} />
          <SettingsSection
            id="hooks"
            title="Git hooks"
            description="After each of these git events this portal rescans the checkout's code structure. It is the one setting a linked project keeps here; the rest belongs to the platform."
          >
            <LinkedHooks slug={slug} readOnly={!configurer} />
          </SettingsSection>
        </>
      ) : (
        <>
          <ConfigForm scope={{ kind: "project", slug }} readOnly={readOnly} />
          {showUsage && <ProjectUsageSection slug={slug} project={p} />}
          {showAgents && <Agents project={p} canSetup={canSetup} />}
          {showAccess && <ProjectAccess slug={slug} />}
          {showConnections && <ProjectConnectedPortals slug={slug} />}
        </>
      )}

      {showDanger && (
        <SettingsSection
          id="danger"
          title="Danger zone"
          danger
          description={
            production
              ? "Removes the server copy and every scan. The repository on GitHub is not changed."
              : linked
                ? "Revokes this machine's access to the platform project and removes its git hooks. The project on the platform and your repository are not changed."
                : "Unregisters the project and removes its git hooks. Your repository and its .whygraph folder are not deleted."
          }
        >
          <div>
            <Button variant="outline" className="text-destructive" onClick={() => setRemoving(true)}>
              {linked ? "Remove from this machine" : "Remove project"}
            </Button>
          </div>
        </SettingsSection>
      )}

      <RemoveProjectDialog project={p} open={removing} onOpenChange={setRemoving} />
    </SettingsLayout>
  );
}
