#!/bin/sh
# The folders a local-mode dev portal shares (`make dev-local` / `dev-docker` /
# `prod`): the scratch repos, then DEV_FOLDERS. Prints them `:`-joined.
#
#   dev_folders.sh DEV_DIR ENV_FILE
#
# DEV_FOLDERS (`:`-separated) comes from the environment when non-empty, else
# from ENV_FILE (`.env.dev`), which is read in a subshell so nothing else in it
# (the GitHub variables) reaches the portal. A folder that would not reach the
# portal intact stops the target instead of being skipped: one that is missing,
# relative, `/`, holds `,` or `"` (they break the shim's --mount), or overlaps
# DEV_DIR (the dev data dirs live there).
set -eu

dev=$1 env_file=$2
scratch="$dev/repos"
NL="$(printf '\n_')"; NL="${NL%_}"

die() { echo "dev folders: $*" >&2; exit 1; }

folders=${DEV_FOLDERS:-}
origin="DEV_FOLDERS"
if [ -z "$folders" ] && [ -f "$env_file" ]; then
    folders=$( set -a; . "$env_file" >/dev/null; printf %s "${DEV_FOLDERS:-}" )
    origin="DEV_FOLDERS in $env_file"
fi

dev_real=$(cd -- "$dev" && pwd -P) || die "cannot resolve '$dev'"
out="$scratch"
rest=$folders
while [ -n "$rest" ]; do
    p=${rest%%:*}
    case "$rest" in *:*) rest=${rest#*:} ;; *) rest="" ;; esac
    [ -n "$p" ] || continue
    case "$p" in
        "~") p=$HOME ;;
        "~/"*) p="$HOME/${p#"~/"}" ;;
    esac
    case "$p" in
        /*) ;;
        *) die "'$p' is not an absolute path ($origin)" ;;
    esac
    [ -d "$p" ] || die "'$p' is not a directory ($origin)"
    real=$(cd -- "$p" && pwd -P) || die "cannot resolve '$p' ($origin)"
    case "$real" in
        *,*|*\"*|*"$NL"*) die "'$real': ',' double quotes and newlines are not allowed in the path ($origin)" ;;
    esac
    [ "$real" != "/" ] || die "refusing to share '/' ($origin)"
    [ "$real" != "$dev_real/repos" ] || continue
    case "$real/" in
        "$dev_real"/*) die "'$real' is inside the dev directory $dev_real ($origin)" ;;
    esac
    case "$dev_real/" in
        "$real"/*) die "'$real' contains the dev directory $dev_real ($origin)" ;;
    esac
    case ":$out:" in *":$real:"*) continue ;; esac
    out="$out:$real"
done
printf '%s\n' "$out"
