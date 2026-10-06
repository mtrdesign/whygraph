import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey, type ProjectSummary } from "../api";
import { useSlug } from "./project";

/** The project actions a role may hold (`PROJECT_ACTIONS` in `portal/authz.py`). */
export type ProjectAction =
  | "project.read"
  | "project.chat"
  | "project.scan"
  | "project.scan_full"
  | "project.configure"
  | "project.setup"
  | "project.access";

/** Every project action: what an admin holds (test fixtures use it as their default). */
export const PROJECT_ACTIONS: ProjectAction[] = [
  "project.read",
  "project.chat",
  "project.scan",
  "project.scan_full",
  "project.configure",
  "project.setup",
  "project.access",
];

/**
 * Whether the caller may do `action` on `project`. Default deny: a project that is
 * not loaded yet, or a payload without `permissions`, allows nothing.
 */
export function can(project: Pick<ProjectSummary, "permissions"> | null | undefined, action: ProjectAction): boolean {
  return !!project?.permissions?.includes(action);
}

/** `can()` for the project `slug` (components that are handed the slug rather than sitting in a project route). */
export function useCanFor(slug: string, action: ProjectAction): boolean {
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  return can(project.data, action);
}

/** `can()` for the current project, read from the cached `project` query (the Explorer and Chat have only the slug). */
export function useProjectCan(action: ProjectAction): boolean {
  return useCanFor(useSlug(), action);
}
