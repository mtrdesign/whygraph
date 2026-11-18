import { ConstructionIcon } from "lucide-react";
import { Link } from "@tanstack/react-router";
import { AdminHeading } from "../components/admin/AdminHeading";
import { PageContainer } from "../components/layout/PageContainer";
import { EmptyState } from "../components/state/EmptyState";
import { Button } from "../components/ui/button";

/**
 * A module route whose screen a later release builds (Projects, Configuration, the
 * detail pages). Never "this release": those modules ship in different ones.
 */
export function AdminPlaceholderPage({ heading }: { heading: string }) {
  return (
    <PageContainer width="narrow" className="flex flex-col gap-4">
      <AdminHeading>{heading}</AdminHeading>
      <EmptyState
        icon={<ConstructionIcon />}
        title="Not available yet"
        description="This part of the admin panel comes in a later release."
        action={
          <Button size="sm" variant="outline" render={<Link to="/admin" />}>
            Back to the dashboard
          </Button>
        }
      />
    </PageContainer>
  );
}
