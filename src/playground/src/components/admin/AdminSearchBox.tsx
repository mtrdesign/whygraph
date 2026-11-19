import { Field } from "../portal/Field";
import { Input } from "../ui/input";

/**
 * A list's search `Input`: filtered on the client as you type. The page keeps the text in the URL
 * (`?q=`, a replace navigation so typing does not fill the history; empty is omitted).
 */
export function AdminSearchBox({ q, onChange }: { q: string | undefined; onChange: (q: string | undefined) => void }) {
  return (
    <Field label="Search" className="w-full sm:max-w-sm">
      {(p) => <Input {...p} type="search" value={q ?? ""} onChange={(e) => onChange(e.target.value || undefined)} />}
    </Field>
  );
}
