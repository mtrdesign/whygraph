import { Link } from "@tanstack/react-router";
import { Button } from "../ui/button";
import { InPage } from "../layout/PageContainer";

export type NotFoundKind = "project" | "run" | "page" | "team-only";

const COPY: Record<NotFoundKind, { title: string; body: string }> = {
  project: {
    title: "Project not found",
    body: "This project may have been removed, or it is restricted and you have no access to it.",
  },
  run: {
    title: "Scan run not found",
    body: "This scan run doesn't exist in this project. It may belong to another project, or the address is wrong.",
  },
  page: { title: "Page not found", body: "There is nothing at this address." },
  "team-only": {
    title: "Only on a team portal",
    body: "This page exists on a team WhyGraph portal. A local portal has one user, so it has no such page.",
  },
};

/**
 * A specific "not found": what is missing, why that can happen, one way back
 * (`back` replaces "Back to projects" where there are no projects, the base host).
 * It renders at the page's position: inside the page's `PageContainer`, or in
 * one of its own when nothing holds it.
 */
export function NotFoundState({
  kind = "page",
  back = { label: "Back to projects", to: "/" },
}: {
  kind?: NotFoundKind;
  back?: { label: string; to: string };
}) {
  const copy = COPY[kind];
  return (
    <InPage>
      <div data-testid="not-found" data-kind={kind}>
        <h1 className="text-lg font-semibold tracking-tight">{copy.title}</h1>
        <p className="mt-1 text-sm text-muted-foreground">{copy.body}</p>
        <div className="mt-3">
          <Button size="sm" variant="outline" render={<Link to={back.to} />}>
            {back.label}
          </Button>
        </div>
      </div>
    </InPage>
  );
}
