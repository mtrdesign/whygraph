import { useSlug } from "../lib/project";
import { ScanHistory } from "../components/portal/ScanHistory";
import { ScanRunView } from "../components/portal/ScanRunView";

/** `/p/<slug>/scans` is the history (screen 8); `/scans/<id>` is one run (screen 7). */
export function ScansPage({ runId }: { runId?: string }) {
  const slug = useSlug();
  const id = runId !== undefined && /^\d+$/.test(runId) ? Number(runId) : null;
  return id === null ? <ScanHistory slug={slug} /> : <ScanRunView key={id} slug={slug} runId={id} />;
}
