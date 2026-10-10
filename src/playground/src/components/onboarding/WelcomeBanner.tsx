import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { onboardingApi, portalKey, type PortalState } from "../../api";
import { usePortalState } from "../../lib/identity";
import { orgRoleLabel } from "../../lib/labels";
import { alertVariants } from "../ui/alert";
import { Button } from "../ui/button";
import { cn } from "@/lib/utils";
import { ConnectAgentDialog } from "./ConnectAgentDialog";

/** The pending dismissal of one org's welcome: kept until the `DELETE` succeeds. */
function pendingKey(org: string, uid: string): string {
  return `whygraph.welcome.dismissed.${org}.${uid}`;
}

function store(key: string, on: boolean): void {
  try {
    if (on) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch {
    // a convenience: the next load asks the server again
  }
}

function stored(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

/**
 * "You've been added to <org> as a Member." (ONB-3), once per membership, in the
 * shell's banner slot. Dismissal is optimistic; a failed `DELETE` is remembered
 * in this browser and retried on the next load, so the banner stays gone.
 */
export function WelcomeBanner() {
  const state = usePortalState().data;
  const welcome = state?.welcome;
  const org = state?.org?.slug ?? "";
  const key = pendingKey(org, state?.user?.uid ?? "-");
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState(false);
  const [hidden, setHidden] = useState(false);

  const dismiss = async () => {
    store(key, true);
    setHidden(true);
    queryClient.setQueryData<PortalState>(portalKey("state"), (s) => (s ? { ...s, welcome: null } : s));
    try {
      await onboardingApi.dismissWelcome();
      store(key, false);
    } catch {
      // kept in storage; retried on the next load
    }
  };

  const retry = !!welcome && stored(key);
  useEffect(() => {
    if (!retry) return;
    onboardingApi
      .dismissWelcome()
      .then(() => store(key, false))
      .catch(() => undefined);
  }, [retry, key]);

  if (!welcome || hidden || retry) return null;
  const role = orgRoleLabel(welcome.role);
  const article = /^[aeiou]/i.test(role) ? "an" : "a";
  return (
    <>
      <div
        role="status"
        data-testid="welcome-banner"
        className={cn(
          alertVariants({ variant: "info" }),
          "flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 rounded-none border-x-0 border-t-0 px-4 py-2 text-[13px]",
        )}
      >
        {/* A 20rem basis: on a phone the buttons wrap under the text instead of
            squeezing it into a narrow column. */}
        <p className="min-w-0 flex-[1_1_20rem]" data-testid="welcome-text">
          <strong className="font-medium">
            You've been added to {welcome.org_name} as {article} {role}.
          </strong>{" "}
          Next: connect your agent so it can use this organization's projects.
        </p>
        <div className="flex shrink-0 gap-2">
          <Button size="sm" onClick={() => setDialog(true)}>
            Connect your agent
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void dismiss()}>
            Dismiss
          </Button>
        </div>
      </div>
      <ConnectAgentDialog open={dialog} onOpenChange={setDialog} />
    </>
  );
}
