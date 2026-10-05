import type { LinkStatus, ProjectLink, ProjectSummary } from "../api";
import { revokedLabel } from "./connections";

/** A `platform` project (local mode): linked to a project on a WhyGraph platform. */
export const isLinked = (p: Pick<ProjectSummary, "source"> | null | undefined): boolean => p?.source === "platform";

/** A URL the server built, only if it is plain http(s) - never `javascript:` or `data:`. */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}

/** `whygraph.example.com` from `https://whygraph.example.com`, for a sentence. */
export function platformHost(origin: string): string {
  try {
    return new URL(origin).host;
  } catch {
    return origin;
  }
}

/** `<platform>/account`, where a connection that could not be revoked from here is revoked. */
export function accountUrl(origin: string | null | undefined): string | undefined {
  const href = safeHref(origin);
  return href ? `${new URL(href).origin}/account` : undefined;
}

/** The local `/link?...` request that reconnects a project to its platform project. */
export function reconnectSearch(link: ProjectLink): { platform: string; org: string; project: string } {
  return { platform: link.platform_origin, org: link.org, project: link.remote_slug };
}

export interface LinkNoticeText {
  tone: "ok" | "warn" | "error";
  title: string;
  /** What to do next, when there is something to do. */
  detail?: string;
  /** Which actions the notice offers. */
  actions: ("reconnect" | "remove" | "manage")[];
}

/**
 * The card's wording per link status (plan section 4.11's table). `link` may be
 * missing on a platform project (an older answer): then only the bare fact shows.
 */
export function linkNotice(link: ProjectLink | null | undefined): LinkNoticeText {
  if (!link) return { tone: "ok", title: "Linked to a platform project", actions: [] };
  const where = `${link.org}/${link.remote_slug}`;
  const host = platformHost(link.platform_origin);
  const status: LinkStatus = link.status;
  switch (status) {
    case "removed":
      return {
        tone: "error",
        title: `Removed on ${host}`,
        detail: "The project no longer exists there. Remove it from this machine.",
        actions: ["remove"],
      };
    case "revoked":
      return {
        tone: "error",
        title: `Access revoked (${link.status_reason ? revokedLabel(link.status_reason) : "no reason given"})`,
        detail: "Reconnect to link this checkout again, or remove it from this machine.",
        actions: ["reconnect", "remove"],
      };
    case "unreachable":
      return {
        tone: "warn",
        title: "Platform unreachable",
        detail: `${host} did not answer. Your agent still sees your local changes; the platform's history returns when it does.`,
        actions: [],
      };
    case "update_required":
      return {
        tone: "warn",
        title: "Update WhyGraph",
        detail: `${host} needs a newer WhyGraph than this portal. Update it with whygraph up.`,
        actions: [],
      };
    case "access_lost":
      return {
        tone: "warn",
        title: `Linked to ${where} on ${host}`,
        detail: "The platform can no longer read the repository, so its history may be out of date. An admin fixes that there.",
        actions: ["manage"],
      };
    default:
      return { tone: "ok", title: `Linked to ${where} on ${host}`, actions: [] };
  }
}
