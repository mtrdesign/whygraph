import { AuthLayout } from "../components/auth/AuthLayout";
import { Button } from "../components/ui/button";
import { baseHostOf, signedInAs, usePortalState, useSignOut } from "../lib/identity";

/** Signed in on an org host the account has no membership in. */
export function NoOrgAccessPage() {
  const state = usePortalState();
  const signOut = useSignOut();
  const base = state.data?.base_url?.replace(/\/$/, "") ?? "";
  // The org the address names: the host without the base host.
  const baseHost = baseHostOf(state.data?.base_url);
  const host = window.location.host;
  const org = baseHost && host.endsWith(`.${baseHost}`) ? host.slice(0, -baseHost.length - 1) : null;
  const login = state.data?.user?.github_login;
  const orgName = org ? <strong>{org}</strong> : "this organization";
  // A platform administrator is not a member: their read-only access is a grant that lapses (D59).
  if (state.data?.user?.is_instance_admin) {
    return (
      <AuthLayout
        title="No access to this organization"
        description={
          <>
            You have no read-only access to {orgName} right now. It lasts 60 minutes from Open as reader, and ends
            when you revoke it.
          </>
        }
      >
        <div className="flex flex-wrap gap-2">
          <Button render={<a href={`${base}/admin${org ? `/orgs/${encodeURIComponent(org)}` : ""}`} />}>
            Open in the admin panel
          </Button>
          <Button variant="outline" onClick={() => void signOut()}>
            Sign out
          </Button>
        </div>
      </AuthLayout>
    );
  }
  return (
    <AuthLayout
      title="No access to this organization"
      description={
        <>
          You are signed in as <strong>{signedInAs(state.data?.user)}</strong>,
          but that account is not a member of {org ? <strong>{org}</strong> : "this organization"}.
        </>
      }
    >
      <p className="text-sm text-muted-foreground" data-testid="join-hint">
        Joining a team? Ask an owner to add{" "}
        {login ? (
          <>
            your GitHub username <span className="font-mono text-foreground">@{login}</span>
          </>
        ) : (
          "you"
        )}
        , then sign in again.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button render={<a href={`${base}/orgs`} />}>Your organizations</Button>
        <Button variant="outline" onClick={() => void signOut()}>
          Sign out
        </Button>
      </div>
    </AuthLayout>
  );
}
