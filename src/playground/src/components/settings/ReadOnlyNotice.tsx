import { EyeIcon } from "lucide-react";
import { platformHost } from "../../lib/platformLink";
import { Alert, AlertDescription } from "../ui/alert";

/** Who is looking at settings they cannot change (SET-4, plan section 4.13). */
export type ReadOnlyAudience =
  | { kind: "project" }
  | { kind: "org" }
  | { kind: "reader" }
  | { kind: "linked"; platformOrigin: string | null | undefined };

const TEST_ID: Record<ReadOnlyAudience["kind"], string> = {
  project: "settings-read-only",
  // The org wording keeps the id the page has always had.
  org: "settings-owner-only",
  reader: "settings-reader",
  linked: "settings-managed",
};

/** The sentence for each audience. */
export function readOnlyText(audience: ReadOnlyAudience): string {
  switch (audience.kind) {
    case "linked": {
      const host = audience.platformOrigin ? platformHost(audience.platformOrigin) : "the platform";
      return `This project is managed on ${host}. Change its settings there.`;
    }
    case "reader":
      return "You're viewing this organization as an instance administrator. Everything here is read-only.";
    case "org":
      return "Only owners can change organization settings.";
    default:
      return "You can view these settings but not change them. Project admins can.";
  }
}

/** The read-only notice above a settings page whose fields are disabled. */
export function ReadOnlyNotice({ audience }: { audience: ReadOnlyAudience }) {
  return (
    <Alert variant="info" data-testid={TEST_ID[audience.kind]}>
      <EyeIcon />
      <AlertDescription>{readOnlyText(audience)}</AlertDescription>
    </Alert>
  );
}
