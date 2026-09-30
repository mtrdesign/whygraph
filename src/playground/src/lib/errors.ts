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
