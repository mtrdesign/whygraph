import { useCallback } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, projectKey, type ChatSession } from "../api";
import { useUi } from "../store";

// The chat session list and its actions (M2f-3 §4.6). They take the slug rather than
// reading it from the project context, so the sidebar's Chats section and the chat
// view share one cache entry (`projectKey(slug, "chat", "sessions")`).

/** The query key of a project's session list. */
export function chatSessionsKey(slug: string) {
  return projectKey(slug, "chat", "sessions");
}

/**
 * The caller's sessions, newest activity first (production lists only their own).
 * `enabled: false` fires no request: a viewer would get a 403.
 */
export function useChatSessions(slug: string, enabled = true) {
  return useQuery<ChatSession[]>({
    queryKey: chatSessionsKey(slug),
    queryFn: () => projectApi(slug).chatSessions(),
    enabled,
  });
}

/** Rename a session; the list refetches on success. */
export function useRenameSession(slug: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: number; title: string }) => projectApi(slug).chatUpdateSession(vars.id, { title: vars.title }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: chatSessionsKey(slug) }),
  });
}

/** Delete a session; the list refetches and the transcript leaves the cache. */
export function useDeleteSession(slug: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => projectApi(slug).chatDeleteSession(id),
    onSuccess: (_data, id) => {
      queryClient.removeQueries({ queryKey: projectKey(slug, "chat", "transcript", id) });
      return queryClient.invalidateQueries({ queryKey: chatSessionsKey(slug) });
    },
  });
}

/**
 * Open a new chat: the empty thread at `/p/$slug/chat`. Nothing is created until
 * the first message is sent (EXC-3), so a click never leaves an empty session behind.
 */
export function useStartChat(slug: string) {
  const navigate = useNavigate();
  const setNavOpen = useUi((s) => s.setNavOpen);
  return useCallback(() => {
    setNavOpen(false);
    void navigate({ to: "/p/$slug/chat/{-$id}", params: { slug, id: undefined } });
  }, [navigate, setNavOpen, slug]);
}
