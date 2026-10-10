import { PageContainer } from "../components/layout/PageContainer";
import { AuditTable } from "../components/portal/AuditTable";
import { usePortalState } from "../lib/identity";

/** `/audit` on an org host (production, owners): the organization's security events. */
export function AuditPage() {
  const org = usePortalState().data?.org;
  return (
    <PageContainer width="default" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Audit log</h1>
        <p className="text-[13px] text-muted-foreground">
          Who did what in {org?.name ?? "this organization"}. Kept for 400 days; nothing secret is stored.
        </p>
      </div>
      <section className="rounded-xl border border-border bg-card p-5 shadow-card">
        <AuditTable scope="org" filename={`whygraph-audit-${org?.slug ?? "org"}.csv`} />
      </section>
    </PageContainer>
  );
}
