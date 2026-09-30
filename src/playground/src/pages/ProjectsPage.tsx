import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, portalKey } from "../api";

// Placeholder for screen 2 (step 12 builds the cards, status badges and empty
// state). It lists the projects so the shell is navigable end to end.
export function ProjectsPage() {
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  return (
    <div className="mx-auto w-full max-w-3xl p-6">
      <h1 className="text-lg font-semibold tracking-tight">Projects</h1>
      {projects.isLoading && <p className="mt-2 text-sm text-muted-foreground">Loading…</p>}
      {projects.isError && (
        <p className="mt-2 text-sm text-destructive">
          Failed to load projects: {projects.error.message}
        </p>
      )}
      {projects.data && projects.data.projects.length === 0 && (
        <p className="mt-2 text-sm text-muted-foreground">
          No projects yet.{" "}
          <Link to="/projects/new" className="text-primary-text underline-offset-4 hover:underline">
            Add project
          </Link>
        </p>
      )}
      <ul className="mt-3 flex flex-col gap-1">
        {projects.data?.projects.map((p) => (
          <li key={p.slug}>
            <Link
              to="/p/$slug"
              params={{ slug: p.slug }}
              className="flex items-center justify-between rounded-md border border-border px-3 py-2 hover:bg-accent"
            >
              <span className="font-medium">{p.name}</span>
              <span className="text-xs text-muted-foreground">{p.source}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
