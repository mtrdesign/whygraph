# Platform administration

Platform administration is for the people who run a production WhyGraph instance. It lives at
`/admin` on the base host, with its own sign-in and its own session, apart from the organizations
people work in. It exists in [production mode](production.md) only; a local portal has no `/admin`
and answers `404` there.

An **instance admin** is a user who has been made an administrator. The first one is the
[bootstrap account](production.md#first-start); every later one is a GitHub user that an
administrator promotes.

## Admin mode

Being an instance admin does not make everyday sessions powerful. Administration uses a **separate
admin session**, started on purpose at `/admin/login`:

- **Two cookies, two jobs.** The everyday session (the one every organization host sees) never opens
  an `/api/admin` route, even when its user is an instance admin. The admin session is a second cookie,
  set on the base host only and never sent to an organization host. A page on an organization cannot
  read or use it.
- **8 hours, no idle limit.** An admin session ends 8 hours after sign-in, whatever you were doing.
  The sidebar shows when ("Session ends ..."). A save after that point answers that the session ended and
  takes you to the sign-in page, so an unsaved edit is lost.
- **It follows the account.** Removing someone's admin flag, disabling them or resetting their password
  ends their admin session on the next request. Signing in again as an administrator replaces your
  previous admin session in that browser.
- **Signing out of the portal ends it too.** An admin session started beside an everyday GitHub
  session ends when you sign out of that everyday session, from an organization host as well. An admin
  session in another browser is left alone.
- **It leaves the everyday session alone.** Signing in to administration does not sign you in to, or
  out of, your organizations. Use **Back to WhyGraph** in the sidebar to return to them.

!!! note "Reading organizations"
    In this release an instance admin who signed in with GitHub can still open any organization
    read-only from the everyday session, as described under [Instance admins](production.md#instance-admins).
    The admin session adds the panel; it does not change that.

## Signing in

Open `<your base URL>/admin`. Without an admin session you are sent to `/admin/login`, and back to
the page you asked for afterwards.

| Way | Who | How |
|---|---|---|
| **Continue with GitHub** | An instance admin with a GitHub account | The same GitHub sign-in as everyone else, including the required two-factor authentication. It only signs in an account that already exists and is an administrator: it never creates an account and never changes a username. Anyone else is refused with "not a platform administrator", and the refusal is written to the [event log](production.md#the-security-event-log). |
| **Sign in with a password** | The bootstrap account (and any password account that is an admin) | Email and password, behind a disclosure on the same page. This is the break-glass way in: a password account has no GitHub account behind it. |

A password account signs in **only here**. It cannot sign in to an organization, so it is for
administration and nothing else; the people who use organizations sign in with GitHub. After the
[bootstrap](production.md#first-start) and after a [password reset](production.md#instance-admins) you
land on `/admin`.

Keep at least two administrators, one of them a GitHub account. The last admin cannot be demoted, and
there is no recovery path for a locked-out one in this release.

## The modules

The nav on the left has four modules in this release. More arrive in later releases and appear in the
nav only when they exist. Press `Cmd-K` (or `Ctrl-K`), or use **Search**, to jump to a page, a user
or an organization.

| Module | What it shows |
|---|---|
| **Dashboard** | The number of users and organizations, the settings check (the base URL and the DNS records the self-check found missing) and the Updates card, which tells administrators when a newer release is out and links its notes. |
| **Organizations** | Every organization: name and slug, its address, its member count and when it was created. |
| **Users** | Every user, with who is an administrator and who is disabled. **Make admin** / **Remove admin** change who administers the instance; **Disable** (after a confirmation) ends the person's sessions at once and refuses their sign-in, **Enable** restores the account; **Copy reset link** issues a one-time password reset link for a password account that is an administrator (see below). You cannot disable your own account. |
| **Audit** | The [security events](production.md#the-security-event-log) that belong to no organization. |

The text on these pages is always shown as plain text, never rendered as markup, because the admin
session shares its origin with pages that show user-supplied names.

### Reset links

There is no email, so an administrator issues a password account's reset link and hands it over out of
band. It works once, for 24 hours, and its token sits in the URL fragment so no proxy log sees it.
Using it sets the new password, ends every session of that account and signs it in to administration.
A password account that is **not** an administrator gets no reset link (`not_an_admin`): promote it
first, or hand its organizations over and delete it (see [Upgrading](../portal/upgrading.md#admin-mode)).

## The Account page

**Account** (the signed-in admin in the sidebar) shows the account, when the admin session started and
when it ends. A password account also changes its password here: **Current password**, **New
password** and **Confirm new password**. The password must be 15 to 256 characters and not a common
one (see [Passwords](production.md#passwords)). Changing it ends every other session of the account
and replaces this one.
