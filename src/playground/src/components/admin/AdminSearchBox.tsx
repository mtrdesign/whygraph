import { useNavigate } from "@tanstack/react-router";
import { Field } from "../portal/Field";
import { Input } from "../ui/input";

/**
 * A list's search `Input`: filtered on the client as you type, and kept in the URL
 * (`?q=`, replace so typing does not fill the history; empty is omitted).
 */
export function AdminSearchBox({ to, q }: { to: "/admin/users" | "/admin/orgs"; q: string | undefined }) {
  const navigate = useNavigate();
  return (
    <Field label="Search" className="max-w-sm">
      {(p) => (
        <Input
          {...p}
          type="search"
          value={q ?? ""}
          onChange={(e) => void navigate({ to, search: { q: e.target.value || undefined }, replace: true })}
        />
      )}
    </Field>
  );
}
