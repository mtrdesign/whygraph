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
