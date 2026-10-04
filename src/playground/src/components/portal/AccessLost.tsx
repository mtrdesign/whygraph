import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { githubApi, type AccessLostReason, type ProjectSummary } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { canAdmin, useRole } from "../../lib/identity";
import { hardNavigate } from "../../lib/navigation";
import { Badge } from "../ui/badge";

/** Why GitHub no longer lets WhyGraph read a production project. */
export const ACCESS_LOST_REASONS: Record<AccessLostReason, string> = {
  no_access: "The WhyGraph app can no longer read this repository.",
  git_access_denied: "GitHub refused git access to this repository.",
  repo_deleted: "The repository was deleted on GitHub.",
  tracked_whygraph_state:
    "The default branch tracks .whygraph/ or .codegraph/. Remove them from the repository to scan again.",
};

/**
 * The access-lost badge of a production project (M2d-2 plan section 0.2 #14):
 * what was scanned stays readable, scans are refused. Owners and admins get
 * "Reconnect on GitHub", the app's install / configure page (a repo that tracks
 * WhyGraph's state is fixed in the repo instead). It clears itself when access
 * returns.
 */
export function AccessLostNotice({ project }: { project: ProjectSummary }) {
  const role = useRole();
  const reconnect = useMutation({
    mutationFn: () => githubApi.authorize(true),
    onSuccess: ({ url }) => void hardNavigate(url),
    onError: (err) => toast.error(authMessage(err)),
  });
  if (!project.access_lost) return null;
  const reason = project.access_lost_reason;
  const text = (reason && ACCESS_LOST_REASONS[reason]) ?? "GitHub no longer lets WhyGraph read this repository.";
  return (
    <div data-testid="access-lost" data-reason={reason ?? ""} className="flex flex-wrap items-center gap-2 text-xs">
      <Badge variant="destructive">Access lost</Badge>
      <span className="text-muted-foreground">{text}</span>
      {canAdmin(role) && reason !== "tracked_whygraph_state" && (
        <button
          type="button"
          className="relative z-10 text-primary-text underline-offset-2 hover:underline"
          disabled={reconnect.isPending}
          onClick={() => reconnect.mutate()}
        >
          Reconnect on GitHub
        </button>
      )}
    </div>
  );
}

/** A local-mode GitHub clone of an older build (plan section 0.2 #16). */
export function UnsupportedSourceNotice({ project }: { project: ProjectSummary }) {
  if (project.source_supported !== false) return null;
  return (
    <p data-testid="source-unsupported" className="text-xs text-warning">
      This source is no longer supported in local mode - remove the project.
    </p>
  );
}
