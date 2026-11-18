import { useQuery } from "@tanstack/react-query";
import { adminApi } from "../api";
import { AdminHeading } from "../components/admin/AdminHeading";
import { SettingsCheck } from "../components/admin/SettingsCheck";
import { UpdatesCard } from "../components/admin/UpdatesCard";
import { PageContainer } from "../components/layout/PageContainer";
import { ErrorState } from "../components/state/ErrorState";
import { Skeleton } from "../components/ui/skeleton";
import { StatTile } from "../components/usage/parts";
import { formatNumber } from "../lib/format";

/** `/admin`: user and organization counts, the base-URL check and the update status. */
export function AdminDashboardPage() {
  const users = useQuery({ queryKey: ["@admin", "users"], queryFn: adminApi.users });
  const orgs = useQuery({ queryKey: ["@admin", "orgs"], queryFn: adminApi.orgs });
  const failed = users.isError ? users : orgs.isError ? orgs : null;
  return (
    <PageContainer width="wide" className="flex flex-col gap-6">
      <AdminHeading>Dashboard</AdminHeading>
      {failed ? (
        <ErrorState
          error={failed.error}
          title="Couldn't load the platform figures"
          onRetry={() => void failed.refetch()}
        />
      ) : users.data && orgs.data ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="admin-tiles">
          <StatTile label="Users" value={formatNumber(users.data.length)} testId="tile-users" />
          <StatTile label="Organizations" value={formatNumber(orgs.data.length)} testId="tile-orgs" />
        </div>
      ) : (
        <Skeleton className="h-16" />
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <SettingsCheck />
        <UpdatesCard />
      </div>
    </PageContainer>
  );
}
