import { useQuery, useQueryClient } from "@tanstack/react-query";
import { authApi, portalApi, portalKey, type PortalState } from "../api";
import { hardNavigate } from "./navigation";

// Helpers for production-mode identity (M2c). Local mode never reaches them: a
// state without `host_kind` is local.

/** The cached portal state (the router's gate has already fetched it). */
export function usePortalState() {
  return useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
}

/**
 * True for an instance admin looking at an org they are not a member of. The
 * backend answers every non-GET with 403 for them; the UI hides the controls.
 */
export function useReadOnly(): boolean {
  return usePortalState().data?.org?.role === "reader";
}

/** True when the state is production's (base or org host). */
export function isProduction(state: PortalState | undefined): boolean {
  return state?.mode === "production" && !!state.host_kind && state.host_kind !== "local";
}

/** `host[:port]` of the base URL, or `""` when it does not parse. */
export function baseHostOf(baseUrl: string | undefined): string {
  try {
    return baseUrl ? new URL(baseUrl).host : "";
  } catch {
    return "";
  }
}

/**
 * Whether `next` could be a sign-in return address: the base URL's scheme and
 * port, on the base host or a subdomain of it. The server validates the same way
 * and falls back to the org picker; this only decides which page the SPA shows.
 */
export function isSafeNext(next: string | undefined, baseUrl: string | undefined): boolean {
  if (!next || !baseUrl) return false;
  try {
    const n = new URL(next);
    const b = new URL(baseUrl);
    return (
      n.protocol === b.protocol &&
      n.port === b.port &&
      (n.hostname === b.hostname || n.hostname.endsWith(`.${b.hostname}`))
    );
  } catch {
    return false;
  }
}

/** `<base>/signin?next=<href>` */
export function signInUrl(baseUrl: string, next: string): string {
  return `${new URL(baseUrl).origin}/signin?next=${encodeURIComponent(next)}`;
}

/**
 * Sign out: revoke the session, refresh the cached state, then leave through the
 * server's redirect (a full page load, so nothing of the old session survives).
 */
export function useSignOut() {
  const queryClient = useQueryClient();
  return async () => {
    const { redirect } = await authApi.logout();
    await queryClient.invalidateQueries({ queryKey: portalKey("state") });
    await hardNavigate(redirect);
  };
}

/** After a successful credential call: refresh the state, then a full navigation. */
export function useFinishAuth() {
  const queryClient = useQueryClient();
  return async (redirect: string) => {
    await queryClient.invalidateQueries({ queryKey: portalKey("state") });
    await hardNavigate(redirect);
  };
}
