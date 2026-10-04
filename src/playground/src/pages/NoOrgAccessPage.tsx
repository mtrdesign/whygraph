import { AuthLayout } from "../components/auth/AuthLayout";
import { Button } from "../components/ui/button";
import { usePortalState, useSignOut } from "../lib/identity";

/** Signed in on an org host the account has no membership in. */
export function NoOrgAccessPage() {
  const state = usePortalState();
  const signOut = useSignOut();
  const base = state.data?.base_url?.replace(/\/$/, "") ?? "";
  return (
    <AuthLayout
      title="No access to this organization"
      description={
        <>
          You are signed in as <strong>{state.data?.user?.email ?? state.data?.user?.display_name}</strong>,
          but that account is not a member of this organization.
        </>
      }
    >
      <div className="flex flex-wrap gap-2">
        <Button render={<a href={`${base}/orgs`} />}>Your organizations</Button>
        <Button variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    </AuthLayout>
  );
}
