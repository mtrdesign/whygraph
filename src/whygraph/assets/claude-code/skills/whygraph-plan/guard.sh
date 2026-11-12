#!/bin/sh
# guard.sh - the Claude Code PreToolUse hook behind the planning harness's agents.
#
#   guard.sh reviewer            Bash: only a plain `git diff ...`
#   guard.sh synthesizer         Bash: only the plan-tool `check` command
#   guard.sh synthesizer-write   Write|Edit: only .whygraph/plans/*.md
#   guard.sh amender-write       Edit: only a *.md file in the project, outside
#                                .git/, .claude/, .cursor/, .github/ and .codex/
#   guard.sh implementor         Bash: anything but a `git push`
#
# Reads the hook's JSON on stdin. Exit 2 blocks, with one line on stderr, and
# exit 0 allows. An unknown mode blocks.
#
# This parses JSON with sed on purpose: a hook must not depend on what the host
# has installed. The first four modes fail closed, so anything the parse
# cannot read is blocked. The implementor mode fails open, so a parse fault
# never stops a build or a test run.

# C locale: sed, tr and grep work on bytes, so a value that is not valid text in
# the host's locale cannot stop them. Nothing here runs a user's command.
LC_ALL=C
export LC_ALL

set -u

block() {
    printf 'guard.sh: %s\n' "$1" >&2
    exit 2
}

# Print the value of a JSON string key. A value holding a backslash does not
# match, so a JSON-escaped newline or quote gives an empty result instead of a
# prefix of the real value. The bracket holds two characters, " and \, because
# a backslash is literal inside a POSIX bracket expression.
extract() {
    printf '%s' "$INPUT" |
        sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"\\]*\)".*/\1/p'
}

# Block a second key of this name, for the fail-closed modes. The greedy sed in
# extract reads the last one, and the tool may run another. Inside a JSON string
# every quote is escaped, so the closing quote of "key" follows a backslash
# there and this pattern only finds real keys. A function that calls exit must
# not run inside $(...), so this is separate from extract.
one_key() {
    case $INPUT in
    *"\"$1\""*"\"$1\""*) block "$2" ;;
    esac
}

# True when the text holds shell syntax. The first seven end or redirect a
# command, or expand to one. A single quote, a glob or a brace can put a
# word on the command line that a check of the literal text never saw:
# '--output=x', {--output=x,HEAD}, or a repo's own file named --output=x.
has_shell_syntax() {
    case $1 in
    *[\;\&\|\<\>\$\`\'\*\?\[\{\}]*) return 0 ;;
    esac
    return 1
}

INPUT=$(cat)

case ${1:-} in
reviewer)
    message='the reviewer may only run a plain git diff'
    one_key command "$message"
    command=$(extract command)
    case $command in
    "git diff "*) ;;
    *) block "$message" ;;
    esac
    has_shell_syntax "$command" && block "$message"
    # Whole words, so the reviewer's own --no-ext-diff does not match. Padded
    # with spaces so the first and last words have a boundary too.
    case " $command " in
    *" --output"[\ =]* | *" --ext-diff"[\ =]* | *" --no-index"[\ =]* | *" --textconv"[\ =]*)
        block "$message"
        ;;
    esac
    ;;
synthesizer)
    message='the synthesizer may only run: sh .claude/skills/whygraph-plan/plan-tool.sh check <path>, the path bare or in single quotes'
    prefix='sh .claude/skills/whygraph-plan/plan-tool.sh check '
    one_key command "$message"
    command=$(extract command)
    case $command in
    "$prefix"*) ;;
    *) block "$message" ;;
    esac
    path=${command#"$prefix"}
    case $path in
    \'*\')
        # The pattern needs two quotes, so a lone ' falls to the bare branch.
        path=${path#\'}
        path=${path%\'}
        ;;
    *\ *)
        block "$message"
        ;;
    esac
    [ -n "$path" ] || block "$message"
    has_shell_syntax "$path" && block "$message"
    ;;
synthesizer-write)
    message='the synthesizer may only write .whygraph/plans/*.md in this project; return the plan to the planner'
    root=${CLAUDE_PROJECT_DIR:-}
    [ -n "$root" ] || block "$message"
    one_key file_path "$message"
    path=$(extract file_path)
    case $path in
    *..*) block "$message" ;;
    # $root is quoted so it is literal. In a case pattern * crosses slashes.
    "$root"/.whygraph/plans/*.md) ;;
    *) block "$message" ;;
    esac
    ;;
amender-write)
    message='the amender may only edit a .md file in this project, outside .git/ and the agent directories'
    root=${CLAUDE_PROJECT_DIR:-}
    [ -n "$root" ] || block "$message"
    one_key file_path "$message"
    path=$(extract file_path)
    case $path in
    *..*) block "$message" ;;
    "$root"/*.md) ;;
    *) block "$message" ;;
    esac
    # A leading / gives the first part a boundary. The brackets also match
    # another case, which reaches the same folder on a case-insensitive disk.
    case /${path#"$root"/} in
    */.[Gg][Ii][Tt]/* | */.[Cc][Ll][Aa][Uu][Dd][Ee]/* | */.[Cc][Uu][Rr][Ss][Oo][Rr]/* | \
        */.[Gg][Ii][Tt][Hh][Uu][Bb]/* | */.[Cc][Oo][Dd][Ee][Xx]/*)
        block "$message"
        ;;
    esac
    ;;
implementor)
    # Drop escaped quotes and turn escaped newlines into separators, so that
    # git commit -m "x" && git push still extracts. Any other escape leaves a
    # backslash, which extracts as empty and is allowed.
    INPUT=$(printf '%s' "$INPUT" | sed 's/\\"//g; s/\\n/;/g')
    command=$(extract command)
    [ -n "$command" ] || exit 0
    # One segment per line. A lone & splits too: it also ends a command. The
    # group is git's own options and their arguments, so `git -C . push` is
    # caught; a first word that is not an option, as in `git stash push`, is not.
    if printf '%s\n' "$command" | tr ';&|' '\n\n\n' | sed 's/^[[:space:]]*//' |
        grep -Eq '^git( -[^ ]+( [^- ][^ ]*)?)* push( |$)'; then
        block 'the implementor may not run git push; leave pushing to the person'
    fi
    ;;
*)
    block 'unknown mode'
    ;;
esac

exit 0
