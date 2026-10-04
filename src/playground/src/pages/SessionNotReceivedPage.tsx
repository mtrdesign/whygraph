import { Link, useSearch } from "@tanstack/react-router";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Button } from "../components/ui/button";
import { usePortalState } from "../lib/identity";

/**
 * Signed in on `/signin` with a `next`: the address you came from did not receive
 * the session cookie (it would have been a sign-in loop). Say so, and offer both
 * ways out.
 */
export function SessionNotReceivedPage() {
  const { next } = useSearch({ strict: false }) as { next?: string };
  const state = usePortalState();
  const who = state.data?.user?.email ?? state.data?.user?.display_name ?? "your account";
  let host = next ?? "";
  try {
    host = new URL(next ?? "").host;
  } catch {
    // Show the raw value.
  }
  return (
    <AuthLayout title="Session not received">
      <p className="text-sm">
        You are signed in as <strong>{who}</strong>, but <span className="font-mono">{host}</span> did not
        receive the session. Check that <span className="font-mono">WHYGRAPH_BASE_URL</span> matches the
        address you use and that your browser accepts cookies for it.
      </p>
      <div className="flex flex-wrap gap-2">
        {next && <Button render={<a href={next} />}>Try {host} again</Button>}
        <Button variant="outline" render={<Link to="/orgs" />}>
          Your organizations
        </Button>
      </div>
    </AuthLayout>
  );
}
