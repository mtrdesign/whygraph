import { useNavigate, useSearch } from "@tanstack/react-router";
import { AdminHeading } from "../components/admin/AdminHeading";
import { PageContainer } from "../components/layout/PageContainer";
import { AuditTable, type AuditUrlFilters } from "../components/portal/AuditTable";
import type { AdminAuditSearch } from "../lib/routeSearch";

/** `/admin/audit`: sign-ins, platform administration and the events of every organization. */
export function AdminAuditPage() {
  const search = useSearch({ strict: false }) as AdminAuditSearch;
  const navigate = useNavigate();
  return (
    <PageContainer width="wide" className="flex flex-col gap-4" data-testid="admin-audit">
      <AdminHeading description="Sign-ins, platform administration and the events of every organization.">
        Audit log
      </AdminHeading>
      <AuditTable
        scope="admin"
        filename="whygraph-admin-audit.csv"
        filters={search}
        onFilters={(next: AuditUrlFilters) =>
          void navigate({
            to: "/admin/audit",
            search: { org: next.org, scope: next.scope, event: next.event, actor: next.actor, from: next.from, to: next.to },
            replace: true,
          })
        }
      />
    </PageContainer>
  );
}
