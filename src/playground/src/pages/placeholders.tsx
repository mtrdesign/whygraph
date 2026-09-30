import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";

// Route components for screens that step 13 builds (scans / settings / project
// overview / edge states). They
// exist now so the route tree, breadcrumbs and sidebar are complete and testable.

function Placeholder({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-3xl p-6">
      <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
      <p className="mt-1 text-sm text-muted-foreground">{children ?? "Coming in a later step."}</p>
    </div>
  );
}

export const GlobalSettingsPage = () => <Placeholder title="Settings" />;
export const ProjectSettingsPage = () => <Placeholder title="Project settings" />;
export const ScansPage = ({ runId }: { runId?: string }) => (
  <Placeholder title={runId ? `Scan run #${runId}` : "Scans"} />
);

export function NotFoundPage() {
  return (
    <div className="mx-auto w-full max-w-3xl p-6">
      <h1 className="text-lg font-semibold tracking-tight">Page not found</h1>
      <p className="mt-1 text-sm text-muted-foreground">
        <Link to="/" className="text-primary-text underline-offset-4 hover:underline">
          Back to projects
        </Link>
      </p>
    </div>
  );
}
