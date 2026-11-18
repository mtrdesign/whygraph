# WhyGraph (unreleased)

Changes that are on `main` and not in a release yet.

!!! warning "Production: hand over the bootstrap account's organizations"
    Password accounts, the bootstrap administrator included, can no longer sign in to an organization;
    they sign in to platform administration at `/admin/login`. If the password account owns an
    organization, make a GitHub user its owner after the upgrade, then remove the password account from
    it. Its connection tokens are revoked. The steps are in
    [Platform administration and password accounts](../portal/upgrading.md#admin-mode). A local portal is
    not affected.

## What's new

- **Platform administration.** Production instances get an admin panel at `/admin` on the base host,
  with its own sign-in (GitHub, or the bootstrap password) and its own 8-hour session that an
  organization host never sees. This release has a Dashboard, Organizations, Users and Audit. See
  [Platform administration](../deploy/administration.md).
- **The password account is for administration only.** `POST /api/auth/login` and the sign-in page's
  "Administrator sign-in" are gone, and so is changing a password from the everyday Account page: it is
  on the admin Account page now.
- **Three new security events**, `admin_signin`, `admin_signin_refused` and `admin_signout`. See
  [the security event log](../deploy/production.md#the-security-event-log).
