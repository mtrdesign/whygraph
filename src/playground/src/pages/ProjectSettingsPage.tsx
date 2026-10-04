import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalApi, portalKey, projectApi, projectKey } from "../api";
import { useReadOnly } from "../lib/identity";
import { useSlug } from "../lib/project";
import { useScanActions } from "../lib/scanActions";
import { ConfigForm } from "../components/portal/ConfigForm";
import { CopyButton } from "../components/portal/CopyButton";
import { NotInitialized, ProjectUnavailable } from "../components/portal/EdgeStates";
import { InitializeStep } from "../components/portal/InitializeStep";
import { ProjectPortChangeNotice } from "../components/portal/PortChangeNotice";
import { RemoveProjectDialog } from "../components/portal/RemoveProjectDialog";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Skeleton } from "../components/ui/skeleton";

const SECTIONS = [
  { id: "general", label: "General" },
  { id: "config", label: "Models, keys and hooks" },
  { id: "agents", label: "Agents" },
  { id: "danger", label: "Danger zone" },
] as const;

function Section({
  id,
  title,
  description,
  danger = false,
  children,
}: {
  id: string;
  title: string;
  description?: string;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      id={`settings-${id}`}
      aria-label={title}
      className={
        danger
          ? "flex scroll-mt-4 flex-col gap-4 rounded-xl border border-destructive/40 bg-card p-5"
          : "flex scroll-mt-4 flex-col gap-4 rounded-xl border border-border bg-card p-5"
      }
    >
      <div>
        <h2 className={danger ? "text-sm font-semibold text-destructive" : "text-sm font-semibold"}>{title}</h2>
        {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function General({ slug }: { slug: string }) {
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const [name, setName] = useState<string | null>(null);
  const { syncNow, syncPending } = useScanActions(slug);
  const readOnly = useReadOnly();
  const rename = useMutation({
    mutationFn: (value: string) => projectApi(slug).rename(value),
    onSuccess: () => {
      setName(null);
      void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") });
      void queryClient.invalidateQueries({ queryKey: portalKey("projects") });
      toast.success("Project renamed");
    },
    onError: (err) => toast.error(err.message),
  });
  const p = project.data;
  if (!p) return <Skeleton className="h-28 rounded-xl" />;
  const value = name ?? p.name;
  const dirty = value.trim() !== p.name && value.trim() !== "";

  return (
    <Section id="general" title="General">
      <form
        className="flex flex-col gap-1.5"
        onSubmit={(e) => {
          e.preventDefault();
          if (dirty) rename.mutate(value.trim());
        }}
      >
        <label htmlFor="project-name" className="text-sm font-medium">
          Display name
        </label>
        <div className="flex gap-2">
          <Input id="project-name" value={value} onChange={(e) => setName(e.target.value)} />
          {!readOnly && (
            <Button type="submit" disabled={!dirty || rename.isPending}>
              Rename
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          The URL name <span className="font-mono">{p.slug}</span> never changes.
        </p>
      </form>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-muted-foreground">Source</dt>
        <dd>{p.source === "github" ? "GitHub clone" : "Local repository"}</dd>
        <dt className="text-muted-foreground">Folder</dt>
        <dd className="font-mono text-xs">{p.root}</dd>
        {p.remote_url && (
          <>
            <dt className="text-muted-foreground">Remote</dt>
            <dd className="font-mono text-xs">{p.remote_url}</dd>
          </>
        )}
        {p.mcp_url && (
          <>
            <dt className="text-muted-foreground">MCP endpoint</dt>
            <dd className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{p.mcp_url}</span>
              <CopyButton text={p.mcp_url} variant="ghost" />
            </dd>
          </>
        )}
      </dl>
      {p.source === "github" && !readOnly && (
        <div className="flex flex-col gap-1.5 border-t border-border pt-4">
          <p className="text-sm font-medium">Sync with GitHub</p>
          <p className="text-xs text-muted-foreground">
            The portal fetches the repository every 15 minutes and scans what is new. Sync now does it
            immediately.
          </p>
          <div>
            <Button
              variant="outline"
              onClick={syncNow}
              disabled={!p.initialized || p.root_status !== "ok" || syncPending}
            >
              Sync now
            </Button>
          </div>
        </div>
      )}
    </Section>
  );
}

/**
 * Screen 10. A sub-nav of sections (§4.12.1): General, the config form (the same
 * `ConfigForm` as the wizard's Configure step, project scope), Agents (add and
 * remove entries, and "Update agent files" with a preview), and a Danger zone
 * with Remove project.
 */
export function ProjectSettingsPage() {
  const slug = useSlug();
  const queryClient = useQueryClient();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  const readOnly = useReadOnly();
  const [removing, setRemoving] = useState(false);
  const p = project.data;
  const scrollTo = (id: string) =>
    document.getElementById(`settings-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="mx-auto flex w-full max-w-5xl gap-8 p-6 sm:p-8">
      <nav aria-label="Settings sections" className="sticky top-6 mt-11 hidden h-fit w-44 shrink-0 flex-col gap-0.5 md:flex">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => scrollTo(s.id)}
            className="rounded-md px-2 py-1.5 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            {s.label}
          </button>
        ))}
      </nav>

      <div className="flex min-w-0 flex-1 flex-col gap-5">
        <h1 className="text-[22px] font-semibold tracking-tight">Settings</h1>
        {p && p.root_status !== "ok" && <ProjectUnavailable project={p} />}
        {p && <ProjectPortChangeNotice slug={slug} change={p.port_change} />}

        <General slug={slug} />

        <div id="settings-config" className="scroll-mt-4">
          <ConfigForm scope={{ kind: "project", slug }} submitLabel="Save settings" />
        </div>

        <Section
          id="agents"
          title="Agents"
          description="The coding agents connected to this project over HTTP. Tick one to add it, untick to remove its entry."
        >
          {!p && <Skeleton className="h-24" />}
          {p && !p.initialized && <NotInitialized slug={slug} />}
          {p && p.initialized && p.root_status === "ok" && !readOnly && (
            <InitializeStep
              slug={slug}
              mode="settings"
              configured={p.agents}
              detected={p.detected ?? null}
              onDone={() => void queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") })}
            />
          )}
        </Section>

        {!readOnly && (
          <Section
            id="danger"
            title="Danger zone"
            description="Unregisters the project and removes its git hooks. Your repository and its .whygraph folder are not deleted."
            danger
          >
            <div>
              <Button variant="destructive" disabled={!p} onClick={() => setRemoving(true)}>
                Remove project
              </Button>
            </div>
          </Section>
        )}
      </div>

      {p && <RemoveProjectDialog project={p} open={removing} onOpenChange={setRemoving} />}
    </div>
  );
}
