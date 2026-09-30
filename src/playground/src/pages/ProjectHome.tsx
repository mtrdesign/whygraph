import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { portalApi, projectKey } from "../api";
import { useSlug } from "../lib/project";

// The project's landing page (screen 9a). Step 13 fills in status, stats, recent
// scans and the MCP connect snippet; for now it names the project and links on.
export function ProjectHome() {
  const slug = useSlug();
  const project = useQuery({
    queryKey: projectKey(slug, "project"),
    queryFn: () => portalApi.project(slug),
  });
  return (
    <div className="mx-auto w-full max-w-3xl p-6">
      <h1 className="text-lg font-semibold tracking-tight">{project.data?.name ?? slug}</h1>
      <p className="mt-1 text-sm text-muted-foreground">Project overview.</p>
      <div className="mt-4 flex gap-3 text-sm">
        <Link to="/p/$slug/explorer" params={{ slug }} className="text-primary-text hover:underline">
          Open Explorer
        </Link>
        <Link
          to="/p/$slug/chat/{-$id}"
          params={{ slug }}
          className="text-primary-text hover:underline"
        >
          Open Chat
        </Link>
      </div>
    </div>
  );
}
