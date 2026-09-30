# Shared folders

The portal runs in Docker, so it does not see your disk - only the folders you **share** with it when
it starts. Each shared folder is mounted into the container at the **same path** it has on your host,
so `/Users/you/Work/api` is `/Users/you/Work/api` inside too. That is what lets the portal, your
editor and your git hooks all agree on where a repository lives.

A local project has to live under a shared folder. A GitHub project added by URL does not: the portal
clones it into its own data directory.

## Share a folder

Share the folder that holds your repositories, not each repository:

```bash
whygraph up --add-folder ~/Work
```

`--add-folder` can be repeated. It resolves the path, removes duplicates, remembers it in
`~/.config/whygraph/folders`, and recreates the container so the new mount takes effect. The portal
keeps your projects and comes back in a few seconds.

```bash
whygraph folders                       # list what is shared
whygraph folders --remove ~/Work       # stop sharing (recreates the container)
```

You rarely type this by hand. If you try to add a repository outside the shared folders, the Add
project screen shows the exact command to run for that path, with a copy button and a **Check again**
button.

## What is refused

`whygraph up --add-folder` rejects a folder, and the portal ignores one, when it:

- is `/`;
- is equal to, inside, or contains the portal's data directory (which holds the stored secrets);
- is not a directory, or contains `:`, `,`, a double quote or a newline in its path.

Sharing your whole home directory is allowed but warned about: every repository under it becomes
visible to the portal. Prefer a narrower folder such as `~/Work`.

## How the portal finds repositories

On the Add project screen the portal lists the git repositories under your shared folders (up to four
levels deep, skipping `node_modules`, `.venv`, `vendor`, `dist` and `build`, and never following
symbolic links), or you can type a path. A typed path is resolved first: `..` and symbolic links
cannot take it outside a shared folder.

## When a folder stops being shared

If you remove a shared folder, or a repository moves or is deleted, the project shows a banner instead
of its data: *The project folder is not available*, with the command to share the folder again. The
project's settings and history stay in the portal until you remove it.
