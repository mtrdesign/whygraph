import { AdminHeading } from "../components/admin/AdminHeading";
import { PageContainer } from "../components/layout/PageContainer";
import { AuditTable } from "../components/portal/AuditTable";

/** `/admin/audit`: sign-ins, instance administration and deleted organizations' events. */
export function AdminAuditPage() {
  return (
    <PageContainer width="wide" className="flex flex-col gap-4" data-testid="admin-audit">
      <AdminHeading description="Sign-ins, platform administration and deleted organizations' events.">
        Audit log
      </AdminHeading>
      <AuditTable scope="admin" />
    </PageContainer>
  );
}
