import { timeAgo } from "../../lib/projectStatus";

/** "{label} {ago}", or nothing when there is no usable time. */
export function When({ label, iso }: { label: string; iso: string | null }) {
  const ago = timeAgo(iso);
  return ago ? (
    <span>
      {label} {ago}
    </span>
  ) : null;
}
