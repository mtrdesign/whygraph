import { useSyncExternalStore } from "react";
import { useRouterState } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { projectKey, type ProjectSummary } from "../../api";
import { WelcomeBanner } from "../onboarding/WelcomeBanner";
import { BudgetBanner, ProjectStopBanner } from "./BudgetBanner";
import { ReaderBanner } from "./ReaderBanner";

/** The project the layout has already loaded (this reads the cache and never fetches). */
function useCachedProject(slug: string | undefined): ProjectSummary | undefined {
  const queryClient = useQueryClient();
  return useSyncExternalStore(
    (onChange) => queryClient.getQueryCache().subscribe(onChange),
    () => (slug ? queryClient.getQueryData<ProjectSummary>(projectKey(slug, "project")) : undefined),
  );
}

/**
 * The one banner slot above every page (USE-1), in a fixed order: Welcome, Reader,
 * Budget. On a project page the Budget group also carries that project's hard-stop
 * notice, except in Chat, which says it in place of its composer (USE-3).
 */
export function BannerStack({ slug }: { slug?: string }) {
  const project = useCachedProject(slug);
  const page = useRouterState({
    select: (s) => (slug ? s.location.pathname.split("/")[3] : undefined),
  });
  const stopped = project?.llm_block === "budget_exceeded";
  return (
    <>
      <WelcomeBanner />
      <ReaderBanner />
      {stopped && page !== "chat" && <ProjectStopBanner scope={project.llm_block_scope} />}
      <BudgetBanner projectStopped={stopped} />
    </>
  );
}
