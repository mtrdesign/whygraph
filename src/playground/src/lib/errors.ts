import { ApiError } from "../api";
import { errorInfo, errorMessage, hasMessage } from "./apiErrors";

/** Where an add-project failure should be shown. */
export interface AddError {
  field: "url" | "token" | "path" | "form";
  message: string;
  /** Set for `not_shared`: the fix the alert shows. */
  command?: string;
  folderSuggestion?: string;
}

/** The form field each add-project code belongs to; anything else is the whole form's. */
const ADD_FIELDS: Record<string, AddError["field"]> = {
  bad_token: "token",
  no_access: "token",
  not_linked: "token",
  not_shared: "path",
  path_missing: "path",
  not_git: "path",
  protected: "path",
};

/**
 * Turn a failed `POST /api/projects` into something the wizard can show: the
 * registry's sentence (`add-project` context) on the field that fixes it. GitHub
 * cannot tell "no such repository" from "a private repository this token cannot
 * see", and neither can we, so `not_found` says both.
 */
export function addProjectError(err: unknown, source: "local" | "github"): AddError {
  const message = errorMessage(err, "add-project");
  if (!(err instanceof ApiError) || !err.code) return { field: "form", message };
  if (err.code === "not_found") return { field: source === "github" ? "url" : "token", message };
  const field = Object.prototype.hasOwnProperty.call(ADD_FIELDS, err.code) ? ADD_FIELDS[err.code] : "form";
  if (err.code === "not_shared") {
    return {
      field,
      message,
      command: typeof err.extra.command === "string" ? err.extra.command : undefined,
      folderSuggestion: typeof err.extra.folder_suggestion === "string" ? err.extra.folder_suggestion : undefined,
    };
  }
  return { field, message };
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
 * agent file, so the only fix is on disk; the body's `path` names it.
 */
export function projectProblem(err: unknown): ProjectProblem {
  const info = errorInfo(err);
  if (err instanceof ApiError && err.code === "unsafe_path") {
    const path = typeof err.extra.path === "string" ? err.extra.path : undefined;
    return {
      kind: "unsafe_path",
      title: info.title,
      message: `${info.message} Replace the link with a real file or folder, then reload.`,
      path,
    };
  }
  if (err instanceof ApiError && err.code === "root_missing") {
    return { kind: "root_missing", title: "The project folder is not available", message: info.message };
  }
  return { kind: "other", title: info.title, message: info.message };
}

/**
 * The registry's sentence for a platform-link or agent-route code (M2e), or
 * `null` for a code the registry does not know. Shared by the platform pages and
 * the local portal's.
 */
export function linkErrorMessage(err: unknown): string | null {
  if (err instanceof ApiError && err.code && hasMessage(err.code)) return errorMessage(err, "link");
  return null;
}

/** The message for a failed platform-link call, in the registry's `link` context. */
export function linkError(err: unknown): string {
  return errorMessage(err, "link");
}
