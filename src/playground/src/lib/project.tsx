import { createContext, useContext, useMemo, type ReactNode } from "react";
import {
  useQuery,
  type QueryKey,
  type UseQueryOptions,
} from "@tanstack/react-query";
import { projectApi, projectKey, type ProjectApi } from "../api";

// The current project is a *slug* handed down by the `/p/$slug` layout route, so
// every project-scoped component reads it from one place. Nothing here caches a
// base URL: `useProjectApi()` is a pure function of the slug, and every query key
// is prefixed with it (`projectKey`), so switching project can only ever read
// cache entries that belong to the new slug.

const SlugContext = createContext<string | null>(null);

export function ProjectProvider({ slug, children }: { slug: string; children: ReactNode }) {
  return <SlugContext.Provider value={slug}>{children}</SlugContext.Provider>;
}

/** The slug of the project the current route is scoped to. */
export function useSlug(): string {
  const slug = useContext(SlugContext);
  if (slug === null) throw new Error("useSlug must be used inside a project route");
  return slug;
}

/** The `projectApi` for the current slug (stable while the slug is). */
export function useProjectApi(): ProjectApi {
  const slug = useSlug();
  return useMemo(() => projectApi(slug), [slug]);
}

/** Builds slug-prefixed query keys, e.g. for `setQueryData` / `invalidateQueries`. */
export function useProjectKey() {
  const slug = useSlug();
  return useMemo(
    () =>
      (...parts: unknown[]): QueryKey =>
        projectKey(slug, ...parts),
    [slug],
  );
}

/**
 * `useQuery` for project data: the key is prefixed with the slug and the fetcher
 * receives that slug's API. Use this instead of `useQuery` for any project call.
 */
export function useProjectQuery<T>(
  parts: unknown[],
  fetcher: (api: ProjectApi) => Promise<T>,
  options: Omit<UseQueryOptions<T, Error, T, QueryKey>, "queryKey" | "queryFn"> = {},
) {
  const slug = useSlug();
  return useQuery<T, Error, T, QueryKey>({
    ...options,
    queryKey: projectKey(slug, ...parts),
    queryFn: () => fetcher(projectApi(slug)),
  });
}
