import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { ApiError, adminApi, type AdminUser } from "../../api";
import { errorInfo } from "../../lib/apiErrors";
import { formatDate } from "../../lib/format";
import { InPage } from "../layout/PageContainer";
import { nativeSelect } from "../portal/Field";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";

/** Every admin mutation invalidates this whole prefix on success (plan 59, §4.15). */
export const ADMIN = ["@admin"] as const;

type Who = Pick<AdminUser, "github_login" | "email" | "uid">;

/** `@login`, else the email, else the uid. */
export function userLabel(u: Who): string {
  return u.github_login ? `@${u.github_login}` : (u.email ?? u.uid);
}

/** What an admin types to confirm a delete or an erase: the login without `@`, else the email, else the uid. */
export function userConfirmValue(u: Who): string {
  return u.github_login ?? u.email ?? u.uid;
}

/** A failure with no dialog to hold it: the registry's title and words as a toast. */
export function toastError(err: unknown): void {
  const info = errorInfo(err);
  toast.error(info.title, { description: info.message });
}

/** Whether `err` is an `ApiError` with this HTTP status. */
export function hasStatus(err: unknown, status: number): boolean {
  return err instanceof ApiError && err.status === status;
}

/** A uid to a display name, from the cached users list ("a platform administrator" when unknown). */
export function useUserName(): (uid: string | null | undefined) => string {
  const users = useQuery({ queryKey: [...ADMIN, "users"], queryFn: adminApi.users });
  const names = new Map((users.data ?? []).map((u) => [u.uid, u.display_name]));
  return (uid) => (uid ? (names.get(uid) ?? "a platform administrator") : "a platform administrator");
}

/** "Deleted <date> by <name>" with its actions: only Restore and Delete permanently (D34). */
export function DeletedAlert({
  deletedAt,
  deletedBy,
  deletedByName,
  children,
}: {
  deletedAt: string;
  deletedBy: string | null;
  /** The deleting user's display label when the answer carries one rather than a uid. */
  deletedByName?: string | null;
  children?: ReactNode;
}) {
  const lookup = useUserName()(deletedBy);
  const name = deletedByName ?? lookup;
  return (
    <Alert variant="warning" data-testid="deleted-alert">
      <AlertDescription>
        Deleted {formatDate(deletedAt)} by {name}
        {children}
      </AlertDescription>
    </Alert>
  );
}

/** A detail page whose slug or uid does not exist: back into the admin app, not out of it. */
export function AdminNotFound({ label, to }: { label: string; to: string }) {
  return (
    <InPage>
      <div data-testid="not-found" data-kind="page">
        <h1 className="text-lg font-semibold tracking-tight">Not found</h1>
        <p className="mt-1 text-sm text-muted-foreground">There is nothing at this address.</p>
        <div className="mt-3">
          <Button size="sm" variant="outline" render={<Link to={to} />}>
            {label}
          </Button>
        </div>
      </div>
    </InPage>
  );
}

/** The labelled "Show" select of a list's status (the `Filter` pattern of `ScanHistory`), "All" being the default. */
export function StatusFilter<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T | undefined;
  onChange: (value: T | undefined) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium sm:w-48">
      Show
      <select
        className={nativeSelect("font-normal")}
        value={value ?? ""}
        onChange={(e) => onChange((e.target.value || undefined) as T | undefined)}
        data-testid="status-filter"
      >
        <option value="">All</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
