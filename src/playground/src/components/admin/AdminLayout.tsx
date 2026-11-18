import { useEffect } from "react";
import { Outlet } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ApiError, adminApi, setAdminSessionSeen } from "../../api";
import { NotFoundPage } from "../portal/EdgeStates";
import { AdminCommandPalette } from "./AdminCommandPalette";
import { AdminShell } from "./AdminShell";
import { PageContainer } from "../layout/PageContainer";
import { ErrorState } from "../state/ErrorState";
import { ForbiddenState } from "../state/ForbiddenState";
import { PageSkeleton } from "../state/Skeletons";
import { isProduction, usePortalState } from "../../lib/identity";
import { useUi } from "../../store";

/** `/admin/*` once the portal is known to be production on the base host. */
function AdminGate() {
  const me = useQuery({ queryKey: ["@admin", "me"], queryFn: adminApi.me });
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  // A palette left open on another page must not greet the admin app.
  useEffect(() => setPaletteOpen(false), [setPaletteOpen]);
  useEffect(() => {
    if (me.data) setAdminSessionSeen(true);
  }, [me.data]);

  if (!me.data) {
    const status = me.error instanceof ApiError ? me.error.status : 0;
    if (me.isError && status === 403) {
      return (
        <PageContainer width="narrow">
          <ForbiddenState what="the admin panel" grant="instance-admin" error={me.error} />
        </PageContainer>
      );
    }
    // A 401 is on its way to `/admin/login` (`failure()` does that redirect, once).
    if (me.isError && status !== 401) {
      return (
        <PageContainer width="narrow">
          <ErrorState error={me.error} size="page" onRetry={() => void me.refetch()} />
        </PageContainer>
      );
    }
    return <PageSkeleton label="Loading administration" />;
  }
  return (
    <AdminShell me={me.data}>
      <Outlet />
      <AdminCommandPalette />
    </AdminShell>
  );
}

/**
 * The admin app's layout. Admin exists only in production, on the base host (local
 * mode answers 404 for `/api/admin/*`), so anywhere else it is "Not found".
 */
export function AdminLayout() {
  const portal = usePortalState().data;
  if (!isProduction(portal) || portal?.host_kind !== "base") {
    return (
      <PageContainer>
        <NotFoundPage />
      </PageContainer>
    );
  }
  return <AdminGate />;
}
