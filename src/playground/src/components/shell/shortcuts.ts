import { useEffect } from "react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { portalApi, projectKey } from "../../api";
import { can } from "../../lib/permissions";
import { useUi } from "../../store";

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * Global keys (§4.12.4): ⌘K / Ctrl-K opens the command menu anywhere, and
 * `g` then a letter jumps to a page (`g p` projects, and inside a project `g o`
 * overview, `g e` explorer, `g c` a new chat, `g s` scans). Ignored while typing.
 * `g e` and `g c` follow the command palette's gates (BUG-18): no Explorer or Chat
 * for a linked project, no Chat without `project.chat`.
 */
export function useGlobalShortcuts() {
  const navigate = useNavigate();
  const { slug } = useParams({ strict: false }) as { slug?: string };
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  // The project layout's query (already cached on a project page).
  const project = useQuery({
    queryKey: projectKey(slug ?? "", "project"),
    queryFn: () => portalApi.project(slug ?? ""),
    enabled: false,
  });
  const linked = project.data?.source === "platform";
  const explorer = !!slug && !!project.data && !linked;
  const chat = explorer && can(project.data, "project.chat");

  useEffect(() => {
    let armed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const disarm = () => {
      armed = false;
      clearTimeout(timer);
    };

    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;

      const key = e.key.toLowerCase();
      if (!armed) {
        if (key === "g") {
          armed = true;
          timer = setTimeout(disarm, 1000);
        }
        return;
      }
      disarm();
      if (key === "p") navigate({ to: "/" });
      else if (slug && key === "o") navigate({ to: "/p/$slug", params: { slug } });
      else if (slug && explorer && key === "e") navigate({ to: "/p/$slug/explorer", params: { slug } });
      else if (slug && chat && key === "c") navigate({ to: "/p/$slug/chat/{-$id}", params: { slug, id: undefined } });
      else if (slug && key === "s") navigate({ to: "/p/$slug/scans/{-$runId}", params: { slug } });
    };

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(timer);
    };
  }, [navigate, slug, setPaletteOpen, explorer, chat]);
}
