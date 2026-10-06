import { ApiError } from "../api";

/** Where an add-project failure should be shown. */
export interface AddError {
  field: "url" | "token" | "path" | "form";
  message: string;
  /** Set for `not_shared`: the fix the alert shows. */
  command?: string;
  folderSuggestion?: string;
}

/**
 * Turn a failed `POST /api/projects` into something the wizard can show. The
 * codes are the portal's (`portal/routes.py` `add_project`); GitHub cannot tell
 * "no such repository" from "a private repository this token cannot see", and
 * neither can we, so `not_found` says both.
 */
export function addProjectError(err: unknown, source: "local" | "github"): AddError {
  if (!(err instanceof ApiError)) {
    return { field: "form", message: err instanceof Error ? err.message : "Something went wrong." };
  }
  switch (err.code) {
    case "bad_token":
      return {
        field: "token",
        message: "GitHub rejected this token. Check that it has not expired or been revoked.",
      };
    case "no_access":
      return {
        field: "token",
        message:
          "This token cannot read the repository. Grant it read access to the repository's contents (and pull requests and issues, for the GitHub crawl).",
      };
    case "not_found":
      return {
        field: source === "github" ? "url" : "token",
        message:
          "Repository not found, or not visible to this token. GitHub reports both the same way.",
      };
    case "invalid_url":
      return {
        field: "url",
        message: "Enter a repository URL like https://github.com/owner/repo.",
      };
    case "not_shared":
      return {
        field: "path",
        message: "This folder is not shared with the portal.",
        command: typeof err.extra.command === "string" ? err.extra.command : undefined,
        folderSuggestion:
          typeof err.extra.folder_suggestion === "string" ? err.extra.folder_suggestion : undefined,
      };
    case "not_git":
      return { field: "path", message: "This folder is not a git repository." };
    case "protected":
      return { field: "path", message: "This path overlaps the portal's own data folder." };
    case "not_linked":
      return {
        field: "token",
        message: "This repository has no GitHub origin, so a GitHub token does not apply.",
      };
    case "duplicate":
      return { field: "form", message: "This repository is already a project." };
    case "clone_failed":
      return { field: "form", message: `Cloning failed: ${err.message}` };
    default:
      return { field: "form", message: err.message };
  }
}

/** A project-scoped failure the UI gives its own wording. */
export interface ProjectProblem {
  kind: "unsafe_path" | "root_missing" | "other";
  title: string;
  message: string;
  /** The offending path, for `unsafe_path`. */
  path?: string;
}

/**
 * Classify a failed project call. `unsafe_path` (409) means the repo holds a
 * symlink where the portal needs a real `.whygraph/`, `.codegraph/`, DB file or
 * agent file, so the only fix is on disk; the backend message already says which
 * path, this adds the instruction.
 */
export function projectProblem(err: unknown): ProjectProblem {
  if (err instanceof ApiError && err.code === "unsafe_path") {
    const path = typeof err.extra.path === "string" ? err.extra.path : undefined;
    return {
      kind: "unsafe_path",
      title: "A symbolic link is in the way",
      message: `${err.message.replace(/\.$/, "")}. Replace the link with a real file or folder, then reload.`,
      path,
    };
  }
  if (err instanceof ApiError && err.code === "root_missing") {
    return {
      kind: "root_missing",
      title: "The project folder is not available",
      message: err.message,
    };
  }
  return {
    kind: "other",
    title: "Something went wrong",
    message: err instanceof Error ? err.message : "The request failed.",
  };
}

/**
 * A human sentence for the codes the platform link and the agent routes raise
 * (M2e). `null` for a code this table does not know, so a caller can fall back to
 * the server's own message. Shared by the platform pages and the local portal's.
 */
const LINK_MESSAGES: Record<string, string> = {
  managed_on_platform: "This project is managed on the platform. Change it there.",
  slug_taken: "A project with this name already exists on this machine. Choose another name.",
  origin_mismatch:
    "This checkout's origin is not the platform project's repository, so it cannot be linked to it.",
  connect_expired: "The connection request expired. Start again from the local portal.",
  link_expired: "The link request expired. Start again from the platform.",
  issuer_mismatch:
    "The reply did not come from the platform you asked to connect to, so it was refused. Start again.",
  access_denied: "The connection was cancelled on the platform.",
  bad_platform_url: "That is not a valid platform address. Use the https address of your WhyGraph platform.",
  bad_platform_reply: "The platform sent an answer WhyGraph could not use. Check the address and try again.",
  bad_connect_request:
    "This connection request is not valid. Start it again from the local portal; do not edit the address.",
  invalid_token: "This connection token is not valid. Link the project again.",
  token_revoked: "This connection was revoked. Link the project again to reconnect.",
  generation_limited: "Your organization's hourly limit for agent-requested generations is used up. Try again later.",
  generation_disabled: "Agent-requested generations are turned off for this organization.",
  no_llm_key: "This project has no LLM key set, so nothing can be generated. Ask an owner to add one.",
  llm_unavailable: "The language model could not be reached. Try again in a moment.",
  busy: "The platform is busy right now. Try again in a moment.",
};

export function linkErrorMessage(err: unknown): string | null {
  if (err instanceof ApiError && err.code && Object.prototype.hasOwnProperty.call(LINK_MESSAGES, err.code)) {
    return LINK_MESSAGES[err.code];
  }
  return null;
}

/** The message for a failed platform-link call: the table above, else the server's own words. */
export function linkError(err: unknown): string {
  return linkErrorMessage(err) ?? (err instanceof Error ? err.message : "Something went wrong.");
}
