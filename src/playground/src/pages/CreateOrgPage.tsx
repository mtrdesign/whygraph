import { useEffect, useState, type FormEvent } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { accountApi, ApiError, onboardingApi, orgsApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { ACCOUNT_ORGS_KEY } from "../components/shell/OrgSwitcher";
import { Button } from "../components/ui/button";
import { DisabledReason } from "../components/state/DisabledReason";
import { Input } from "../components/ui/input";
import { authMessage } from "../lib/authErrors";
import { baseHostOf, useFinishAuth, usePortalState } from "../lib/identity";

const SLUG_RULE = "1-40 lower-case letters, digits and dashes, starting and ending with a letter or digit.";

/** A starting slug from the display name; the user can edit it. */
function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40)
    .replace(/-+$/, "");
}

const SLUG_REASON: Record<string, string> = {
  invalid: "is not a valid URL name",
  reserved: "is reserved",
  taken: "is already taken",
};

/** `value`, after it stopped changing for `ms` (the slug check waits for the typing to pause). */
function useDebounced(value: string, ms: number): string {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return out;
}

/** `<base>/orgs/new`: any signed-in user can create an organization and becomes its owner. */
export function CreateOrgPage() {
  const state = usePortalState();
  const host = baseHostOf(state.data?.base_url);
  const finish = useFinishAuth();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);

  const debounced = useDebounced(slug, 300);
  const check = useQuery({
    queryKey: ["@slug-check", debounced],
    queryFn: () => onboardingApi.slugCheck(debounced),
    enabled: !!debounced && debounced === slug,
    retry: false,
    staleTime: 30_000,
  });
  // Only an answer for the slug on screen counts; a throttled or failed check says nothing.
  const checked = check.data && check.data.slug === slug ? check.data : null;
  const orgs = useQuery({ queryKey: ACCOUNT_ORGS_KEY, queryFn: accountApi.orgs, staleTime: 0, retry: false });
  const login = state.data?.user?.github_login;

  const create = useMutation({
    mutationFn: () => orgsApi.create({ slug, name: name.trim() }),
    onSuccess: ({ url }) => finish(url),
  });
  const code = create.error instanceof ApiError ? create.error.code : undefined;
  const slugError =
    code === "slug_taken"
      ? "That URL name is already taken."
      : code === "bad_slug"
        ? authMessage(create.error)
        : undefined;
  // The live check refused the slug on screen: no "your address will be" preview,
  // and Create is disabled with the check's reason (a known-bad submit is not offered).
  const refused = checked && !checked.available ? `${slug}.${host} ${SLUG_REASON[checked.reason ?? "invalid"] ?? "is not available"}` : null;
  const disabledReason = !name.trim()
    ? "Enter the organization's name."
    : !slug
      ? "Enter a URL name."
      : refused
        ? `Choose another URL name: ${refused}.`
        : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (refused) return;
    create.mutate();
  };

  return (
    <AuthLayout
      title="Create an organization"
      description="An organization holds your projects and members. You become its owner."
      footer={
        <Link to="/orgs" className="text-primary-text hover:underline">
          Back to your organizations
        </Link>
      }
    >
      {login && orgs.isSuccess && orgs.data.length === 0 && (
        <p className="rounded-lg bg-muted px-3 py-2 text-sm text-muted-foreground" data-testid="join-hint">
          Joining a team? You don't need your own organization. Ask an owner to add your GitHub username{" "}
          <span className="font-mono text-foreground">@{login}</span>, then sign in again.
        </p>
      )}
      <form noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <Field label="Organization name">
          {(p) => (
            <Input
              {...p}
              autoFocus
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!slugEdited) setSlug(suggestSlug(e.target.value));
              }}
            />
          )}
        </Field>
        <Field
          label="URL name"
          hint={
            refused ? (
              `Use ${SLUG_RULE}`
            ) : (
              <>
                Your address will be{" "}
                <span className="font-mono" data-testid="slug-preview">
                  {slug || "your-org"}.{host}
                </span>
                . Use {SLUG_RULE}
              </>
            )
          }
          error={slugError}
        >
          {(p) => (
            <Input
              {...p}
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              value={slug}
              onChange={(e) => {
                setSlug(e.target.value.toLowerCase());
                setSlugEdited(true);
              }}
            />
          )}
        </Field>
        {checked && !slugError && (
          <p
            role="status"
            data-testid="slug-status"
            className={checked.available ? "text-xs text-success" : "text-xs text-destructive"}
          >
            {checked.available ? `${slug}.${host} is available` : refused}
          </p>
        )}
        {create.isError && !slugError && (
          <Alert variant="destructive">
            <AlertDescription>{authMessage(create.error)}</AlertDescription>
          </Alert>
        )}
        {disabledReason && !create.isPending ? (
          <DisabledReason reason={disabledReason}>
            <Button type="submit" disabled className="w-full">
              Create organization
            </Button>
          </DisabledReason>
        ) : (
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create organization"}
          </Button>
        )}
      </form>
    </AuthLayout>
  );
}
