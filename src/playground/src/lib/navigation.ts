/**
 * A full-page navigation (cross-host redirects: sign-in, org switch, sign-out).
 * Returns a promise that never resolves, so a router `beforeLoad` that awaits it
 * stops there instead of rendering a page the browser is about to leave. Tests
 * mock this module (jsdom cannot navigate).
 */
export function hardNavigate(url: string): Promise<never> {
  window.location.assign(url);
  return new Promise<never>(() => {});
}
