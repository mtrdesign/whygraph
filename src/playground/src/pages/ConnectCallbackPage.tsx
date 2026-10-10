import { PageContainer } from "../components/layout/PageContainer";
import { useEffect, useRef } from "react";
import { Link, useRouter, useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { platformApi } from "../api";
import { linkError } from "../lib/errors";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";

export interface CallbackSearch {
  code?: string;
  state?: string;
  iss?: string;
  error?: string;
}

/**
 * `/connect/callback`: where the platform sends the browser back. It hands what
 * it received to `POST /api/platform/callback` exactly once (the portal's pending
 * connect is the only state check - a repeat answers `connect_expired`) and then
 * `replace`s to the wizard's checkout step, so Back never re-posts the code.
 */
export function ConnectCallbackPage() {
  const router = useRouter();
  const search = useSearch({ strict: false }) as CallbackSearch;
  const sent = useRef(false);
  const finish = useMutation({
    mutationFn: () =>
      platformApi.callback({
        state: search.state ?? "",
        ...(search.iss !== undefined && { iss: search.iss }),
        ...(search.code !== undefined && { code: search.code }),
        ...(search.error !== undefined && { error: search.error }),
      }),
    onSuccess: ({ link_id }) =>
      router.history.replace(`/projects/new?${new URLSearchParams({ source: "platform", link: link_id })}`),
  });

  useEffect(() => {
    if (sent.current || !search.state) return;
    sent.current = true;
    finish.mutate();
    // The effect runs once per mount; `finish` is stable enough and must not re-fire.
  }, []);

  return (
    <PageContainer className="flex flex-col gap-4 max-w-[640px]">
      {!search.state ? (
        <Alert variant="destructive" data-testid="callback-invalid">
          <AlertTitle>Nothing to finish</AlertTitle>
          <AlertDescription>This page is where a platform sends you back after you allow a connection.</AlertDescription>
        </Alert>
      ) : finish.isError ? (
        <Alert variant="destructive" data-testid="callback-error">
          <AlertTitle>The connection was not completed</AlertTitle>
          <AlertDescription>{linkError(finish.error)}</AlertDescription>
        </Alert>
      ) : (
        <p className="text-sm text-muted-foreground" data-testid="callback-working">
          Finishing the connection…
        </p>
      )}
      {(!search.state || finish.isError) && (
        <div>
          <Button variant="outline" render={<Link to="/projects/new" search={{ source: "platform" }} />}>
            Start again
          </Button>
        </div>
      )}
    </PageContainer>
  );
}
