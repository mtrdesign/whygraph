import type { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { ApiError } from "../../api";
import type { ErrorContext } from "../../lib/apiErrors";
import { ErrorState } from "./ErrorState";
import { ForbiddenState, type Grantor } from "./ForbiddenState";
import { NotFoundState, type NotFoundKind } from "./NotFoundState";

/**
 * One gate for a query's states, so a page (or a form that needs loaded data)
 * never half-renders (M2f-3 ER-4): `loading` while there is no data, a 403 as
 * `ForbiddenState`, a 404 as `NotFoundState`, any other error as `ErrorState`
 * with Retry, `empty` when `isEmpty(data)`, else `children(data)`.
 */
export function QueryState<T>({
  query,
  loading,
  isEmpty,
  empty,
  errorTitle,
  context,
  forbidden,
  notFound = "page",
  children,
}: {
  query: Pick<UseQueryResult<T>, "data" | "error" | "isError" | "refetch">;
  /** The skeleton in the page's shape. */
  loading: ReactNode;
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  /** "Couldn't load scans". */
  errorTitle?: string;
  context?: ErrorContext;
  /** What a 403 denies, and who can grant it. */
  forbidden?: { what: string; grant?: Grantor };
  /** Which "not found" a 404 is. */
  notFound?: NotFoundKind;
  children: (data: T) => ReactNode;
}) {
  if (query.data === undefined) {
    if (!query.isError) return <>{loading}</>;
    const error = query.error;
    if (error instanceof ApiError && error.status === 403) {
      return <ForbiddenState what={forbidden?.what ?? "this page"} grant={forbidden?.grant} error={error} />;
    }
    if (error instanceof ApiError && error.status === 404) return <NotFoundState kind={notFound} />;
    return (
      <ErrorState error={error} title={errorTitle} context={context} onRetry={() => void query.refetch()} />
    );
  }
  if (isEmpty?.(query.data)) return <>{empty ?? null}</>;
  return <>{children(query.data)}</>;
}
