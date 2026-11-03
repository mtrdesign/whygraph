import { useState } from "react";
import { dismissUpdate, isUpdateDismissed } from "../../lib/updates";
import { usePortalState } from "../../lib/identity";
import { CopyButton } from "../portal/CopyButton";
import { BannerStrip } from "./BudgetBanner";

const linkClass = "font-medium underline underline-offset-2 hover:text-foreground";

function ExternalLink({ href, children }: { href: string; children: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer noopener" className={linkClass}>
      {children}
    </a>
  );
}

/**
 * "A newer WhyGraph is available" (plan 43), from `state.update`. Dismissible per
 * version; a release that needs manual steps points at the upgrade guide instead of
 * offering the `whygraph update` command.
 */
export function UpdateBanner() {
  const update = usePortalState().data?.update;
  const [hidden, setHidden] = useState<string | null>(null);
  const latest = update?.latest;
  if (!update?.available || !latest || hidden === latest || isUpdateDismissed(latest)) return null;
  return (
    <BannerStrip
      tone="info"
      testId="update-banner"
      dismissLabel="Dismiss"
      onDismiss={() => {
        dismissUpdate(latest);
        setHidden(latest);
      }}
      action={
        <>
          {update.manual_steps && <ExternalLink href={update.upgrading_url}>How to upgrade</ExternalLink>}
          {update.notes_url && <ExternalLink href={update.notes_url}>What's new</ExternalLink>}
        </>
      }
    >
      {update.manual_steps ? (
        <span>
          <strong>WhyGraph {latest} is available and needs manual steps.</strong>
        </span>
      ) : (
        <>
          <span>
            <strong>
              WhyGraph {latest} is available{update.current ? ` (you have ${update.current})` : ""}.
            </strong>{" "}
            Run <code>whygraph update</code> in a terminal.
          </span>
          <CopyButton text="whygraph update" variant="ghost" />
        </>
      )}
    </BannerStrip>
  );
}
