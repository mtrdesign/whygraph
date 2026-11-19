import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { adminApi } from "../../api";
import { errorMessage } from "../../lib/apiErrors";
import { DisabledReason } from "../state/DisabledReason";
import { TypedConfirmDialog } from "../portal/TypedConfirmDialog";
import { Button } from "../ui/button";
import { ADMIN, toastError } from "./adminParts";

const DESTRUCTIVE = "text-destructive";

/** What the state badge reads: the project's own fields, from a list row, an org's tab or a detail page. */
export interface ProjectStateInput {
  deleted_at: string | null;
  access_lost_reason: string | null;
  last_run_status: string | null;
  last_scan_at: string | null;
}

export type ProjectStateKey = "deleted" | "access_lost" | "failing" | "never_scanned" | "ok";

export function projectState(p: ProjectStateInput): {
  key: ProjectStateKey;
  label: string;
  variant: "destructive" | "outline" | "secondary";
} {
  if (p.deleted_at) return { key: "deleted", label: "Deleted", variant: "destructive" };
  if (p.access_lost_reason) return { key: "access_lost", label: "Access lost", variant: "destructive" };
  if (p.last_run_status === "failed" || p.last_run_status === "interrupted") return { key: "failing", label: "Failing", variant: "destructive" };
  if (!p.last_scan_at) return { key: "never_scanned", label: "Never scanned", variant: "outline" };
  return { key: "ok", label: "OK", variant: "secondary" };
}

/** An org's projects: a live one has Delete, a soft-deleted one Restore and Delete permanently (D34). */
export function ProjectActions({
  org,
  project,
}: {
  org: { slug: string; name: string; deleted_at: string | null };
  project: { slug: string; name: string; deleted_at: string | null };
}) {
  const queryClient = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const [purging, setPurging] = useState(false);
  const done = () => queryClient.invalidateQueries({ queryKey: ADMIN });
  const remove = useMutation({
    mutationFn: () => adminApi.deleteProject(org.slug, project.slug, project.name),
    onSuccess: async () => {
      setDeleting(false);
      await done();
    },
  });
  const purge = useMutation({
    mutationFn: () => adminApi.purgeProject(org.slug, project.slug, project.name),
    onSuccess: async () => {
      setPurging(false);
      await done();
    },
  });
  const restore = useMutation({
    mutationFn: () => adminApi.restoreProject(org.slug, project.slug),
    onSuccess: done,
    onError: toastError,
  });
  const orgGone = !!org.deleted_at;
  const restoreButton = (
    <Button size="sm" variant="secondary" disabled={orgGone || restore.isPending} onClick={() => restore.mutate()}>
      Restore
    </Button>
  );
  return (
    <span className="flex flex-wrap items-center gap-2">
      {project.deleted_at ? (
        <>
          {orgGone ? <DisabledReason reason="Restore the organization first.">{restoreButton}</DisabledReason> : restoreButton}
          <Button
            size="sm"
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              purge.reset();
              setPurging(true);
            }}
          >
            Delete permanently
          </Button>
        </>
      ) : (
        !orgGone && (
          <Button
            size="sm"
            variant="outline"
            className={DESTRUCTIVE}
            onClick={() => {
              remove.reset();
              setDeleting(true);
            }}
          >
            Delete
          </Button>
        )
      )}
      <TypedConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        testId="delete-project-dialog"
        title={`Delete ${project.name}?`}
        description={`${project.name} disappears for everyone in ${org.name} at once, and its scans stop. Nothing is erased: you can restore it, or delete it permanently later.`}
        expected={project.name}
        confirmLabel="Delete project"
        pendingLabel="Deleting..."
        pending={remove.isPending}
        error={remove.isError ? errorMessage(remove.error) : null}
        onConfirm={() => remove.mutate()}
      />
      <TypedConfirmDialog
        open={purging}
        onOpenChange={setPurging}
        testId="purge-project-dialog"
        title={`Delete ${project.name} permanently?`}
        description="Its server copy and its history (descriptions, rationale cards, chats) are deleted. This cannot be undone. The repository on GitHub is not changed."
        expected={project.name}
        confirmLabel="Delete permanently"
        pendingLabel="Deleting..."
        pending={purge.isPending}
        error={purge.isError ? errorMessage(purge.error) : null}
        onConfirm={() => purge.mutate()}
      />
    </span>
  );
}
