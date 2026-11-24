#!/bin/sh
# claude-md-tool.sh - the deterministic parts of /whygraph-claude-md.
#
#   draft-dir                   create .whygraph/claude-md/draft/
#   clean                       empty .whygraph/claude-md/draft/; keep
#                               backup/ and proposed.txt
#   inventory                   one line per instruction file: path, lines,
#                               bytes, comment bytes, longest line, state
#                               (tracked, dirty, untracked)
#   signals                     print the capped digest of the repository
#   check <file> root|nested    lint one CLAUDE.md or draft; one finding per
#                               line: "fail|warn <rule>: <line>: <detail>"
#                               or "info foreign-block: <name> <bytes>"
#                               (line 0 is the whole file)
#   apply <draft> [--if-clean]  check one draft, back up its target, write it
#
# Exit codes: 0 done (check: clean or warnings only), 1 a check fail,
# 2 usage, 3 a refused path, 4 --if-clean and the target is untracked or
# dirty. Run from anywhere inside the repository; the root is
# `git rev-parse --show-toplevel`. Tables are tab-separated. POSIX sh, git,
# sed, awk and grep only. Repository text is data: this script never runs it.

set -u
LC_ALL=C
export LC_ALL

TAB=$(printf '\t')
NL='
'
WG=.whygraph/claude-md
DRAFT=$WG/draft
BACKUP=$WG/backup
MANIFESTS='package.json pyproject.toml Cargo.toml go.mod composer.json Gemfile pom.xml build.gradle'

# The secret shapes: TOKEN_PATTERN and PROVIDER_KEY_PATTERN of
# services/git/credentials.py, then other common credentials.
SECRET_RE='gh[opsur]_[A-Za-z0-9_.-]{8,}|wgc_[A-Za-z0-9_-]{8,}|(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^/ :@]+:[^/ @]+@'

usage() {
    printf 'claude-md-tool: %s\n' "$1" >&2
    exit 2
}

refuse() {
    printf 'claude-md-tool: refused: %s\n' "$1" >&2
    exit 3
}

g() { git -C "$ROOT" -c core.quotePath=false "$@"; }
gl() { git --literal-pathspecs -C "$ROOT" -c core.quotePath=false "$@"; }

# ---- paths ----------------------------------------------------------------

# Refuse when any component of the root-relative path $1 is a symlink.
no_links() {
    nl_acc=""
    nl_ifs=$IFS
    IFS=/
    set -f
    for nl_c in $1; do
        nl_acc=${nl_acc:+$nl_acc/}$nl_c
        if [ -L "$ROOT/$nl_acc" ]; then
            IFS=$nl_ifs
            set +f
            refuse "$nl_acc is a symlink"
        fi
    done
    IFS=$nl_ifs
    set +f
}

# Set REL to the path $1 relative to the root, lexically normalized.
# Refuse a newline, a path outside the root and a symlink in any component.
to_rel() {
    case $1 in
        "") usage "an empty path" ;;
        *"$NL"*) refuse "the path holds a newline" ;;
    esac
    case $1 in
        /*) tr_abs=$1 ;;
        *) tr_abs=$(pwd -P)/$1 ;;
    esac
    tr_abs=$(P="$tr_abs" awk 'BEGIN {
        n = split(ENVIRON["P"], c, "/"); k = 0
        for (i = 1; i <= n; i++) {
            if (c[i] == "" || c[i] == ".") continue
            if (c[i] == "..") { if (k > 0) k--; continue }
            s[++k] = c[i]
        }
        o = ""; for (i = 1; i <= k; i++) o = o "/" s[i]
        print (o == "" ? "/" : o)
    }')
    case $tr_abs in
        "$ROOT"/?*) REL=${tr_abs#"$ROOT"/} ;;
        *) refuse "$1 is outside the repository" ;;
    esac
    no_links "$REL"
}

# Refuse a target path (root-relative) that the allowlist does not cover.
allow() {
    al_t=$1
    no_links "$al_t"
    case ${al_t##*/} in
        CLAUDE.md) ;;
        *) refuse "$al_t: the file name is not CLAUDE.md" ;;
    esac
    case $al_t in
        .git/* | .claude/* | .whygraph/* | .cursor/* | .github/* | .codex/*)
            refuse "$al_t: inside ${al_t%%/*}/" ;;
    esac
    if [ -e "$ROOT/.claude/CLAUDE.md" ] && [ "$ROOT/$al_t" -ef "$ROOT/.claude/CLAUDE.md" ]; then
        refuse "$al_t: the same file as .claude/CLAUDE.md"
    fi
    case $al_t in
        */*) al_dir=${al_t%/*} ;;
        *) return 0 ;;
    esac
    if g ls-files -s | T="$al_t" awk -F"$TAB" '
        { split($1, m, " ") }
        m[1] == "160000" && index(ENVIRON["T"], $2 "/") == 1 { f = 1 }
        END { exit !f }'; then
        refuse "$al_t: inside a submodule"
    fi
    al_acc=""
    al_ifs=$IFS
    IFS=/
    set -f
    for al_c in $al_dir; do
        al_acc=${al_acc:+$al_acc/}$al_c
        if [ -e "$ROOT/$al_acc/.git" ]; then
            IFS=$al_ifs
            set +f
            refuse "$al_t: inside a nested repository at $al_acc"
        fi
    done
    IFS=$al_ifs
    set +f
    if [ -z "$(gl ls-files -- "$al_dir/" | head -n 1)" ]; then
        refuse "$al_t: $al_dir holds no tracked file"
    fi
}

# Print the target a root-relative path stands for: a draft stands for the
# path after the draft prefix.
target_of() {
    case $1 in
        "$DRAFT"/?*) printf '%s\n' "${1#"$DRAFT"/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Refuse when .whygraph, .whygraph/claude-md or the draft directory is a
# symlink, a tracked path, or not a directory.
work_dirs_ok() {
    for wd in .whygraph "$WG" "$DRAFT" "$BACKUP"; do
        if [ -L "$ROOT/$wd" ]; then
            refuse "$wd is a symlink"
        fi
        if [ -e "$ROOT/$wd" ] && [ ! -d "$ROOT/$wd" ]; then
            refuse "$wd is not a directory"
        fi
        case $wd in
            .whygraph) wd_hit=$(gl ls-files -- "$wd" | grep -x -F -e "$wd") ;;
            *) wd_hit=$(gl ls-files -- "$wd" | head -n 1) ;;
        esac
        if [ -n "$wd_hit" ]; then
            refuse "$wd is a tracked path"
        fi
    done
}

# Print tracked, dirty or untracked for the root-relative path $1.
state_of() {
    if ! gl ls-files --error-unmatch -- "$1" >/dev/null 2>&1; then
        echo untracked
    elif [ -n "$(gl status --porcelain -- "$1")" ]; then
        echo dirty
    else
        echo tracked
    fi
}

# ---- the Markdown reader --------------------------------------------------

# One pass over a CLAUDE.md. mode=check prints findings (and "cand" lines for
# dead-path); mode=bullets prints each bullet's text; mode=stats prints the
# comment bytes and the longest line.
AWK_MD='
function clen(s,   t, n) { t = s; n = gsub(/[\200-\277]/, "", t); return length(s) - n }
function out(lv, rule, ln, d) { if (mode == "check") print lv " " rule ": " ln ": " d }
function endsec(   need) {
    need = bul
    if (need == 0 && stitle && prose > 0) need = 1
    if (!srcs) { if (need > 0) out("fail", "sources", sline, "no sources comment for this section") }
    else if (ents != need) out("fail", "sources", srcline, ents " entries for " need " bullets")
}
function srcentry(l,   num) {
    if (l ~ /^[ \t]*-->[ \t]*$/ || l ~ /^[ \t]*$/) return
    ents++
    if (index(l, "--")) { out("fail", "sources", NR, "an entry holds --"); return }
    if (l !~ /^[0-9]+: [A-Za-z0-9 #:,._\/-]+$/) { out("fail", "sources", NR, "an entry is not <n>: <source>"); return }
    num = l; sub(/:.*/, "", num)
    if (num + 0 != ents) out("fail", "sources", NR, "entry " num " is out of order, expected " ents)
}
function pathlike(w) {
    if (w == "" || w ~ /[<>*${:]/ || w ~ /^(\/|-|http)/) return 0
    if (index(w, "/")) return 1
    return (w ~ /\.(md|py|ts|tsx|js|jsx|mjs|json|toml|yml|yaml|sh|lock|cfg|ini|sql|go|rs)$/)
}
function spans(l,   n, p, i, w, nw, j) {
    n = split(l, p, "`")
    for (i = 2; i < n; i += 2) {
        nw = split(p[i], w, /[ \t]+/)
        for (j = 1; j <= nw; j++) if (pathlike(w[j]) && mode == "check") print "cand\t" NR "\t" w[j]
    }
}
function emphasis(l,   s) {
    s = l; gsub(/`[^`]*`/, "", s)
    if (s ~ /(^|[^A-Za-z0-9_])(IMPORTANT|NEVER|ALWAYS|CRITICAL)([^A-Za-z0-9_]|$)/ || s ~ /(^|[^A-Za-z0-9_])YOU[ \t]+MUST([^A-Za-z0-9_]|$)/)
        out("fail", "emphasis", NR, "a word in capitals for emphasis")
}
BEGIN {
    if (kind == "root") { maxl = 200; maxb = 12288 } else { maxl = 80; maxb = 6144 }
    names["Commands"]; names["Verify"]; names["Boundaries"]; names["Traps"]
    names["Conventions"]; names["Where things live"]
    if (dupfile != "") while ((getline d < dupfile) > 0) dup[d] = 1
    B1 = "\342\224\234"; B2 = "\342\224\224"; B3 = "\342\224\202"
    sline = 1; prevblank = 1
}
{
    raw = $0; line = raw; sub(/\r$/, "", line)
    if (clen(line) > longest) longest = clen(line)
    if (infor) {
        fb += length(raw) + 1
        if (line ~ fend) { infor = 0; if (mode == "check") print "info foreign-block: " fname " " fb }
        next
    }
    if (incom) {
        cb += length(raw) + 1
        if (insrc) srcentry(line)
        if (index(line, "-->")) { incom = 0; insrc = 0 }
        prevblank = 0; next
    }
    if (!incode && line ~ /^<!-- *([A-Z0-9_]+_START|BEGIN [A-Za-z0-9_-]+) *-->/) {
        t = line; sub(/^<!-- */, "", t); sub(/ *-->.*$/, "", t)
        if (t ~ /_START$/) { fname = substr(t, 1, length(t) - 6); fend = "^[ ]*<!-- *" fname "_END *-->" }
        else { fname = substr(t, 7); fend = "^[ ]*<!-- *END " fname " *-->" }
        fb = length(raw) + 1; infor = 1; prevblank = 0; next
    }
    if (!incode && line ~ /^[ ]*<!--/) {
        cb += length(raw) + 1
        if (line ~ /^[ ]*<!--[ ]*whygraph:sources/) {
            if (srcs) out("fail", "sources", NR, "a second sources comment in this section")
            srcs = 1; srcline = NR; ents = 0; insrc = 1
            if (line !~ /^<!--/) out("fail", "sources", NR, "the sources comment does not start at column 0")
            if (!prevblank) out("fail", "sources", NR, "no blank line before the sources comment")
        }
        t = line; sub(/^[ ]*<!--/, "", t)
        if (index(t, "-->")) insrc = 0
        else incom = 1
        prevblank = 0; next
    }
    cnt++; bytes += length(raw) + 1
    if (index(line, B1) || index(line, B2) || index(line, B3)) out("fail", "tree", NR, "a box-drawing tree character")
    if (line ~ /^[ \t]*(- )?[^ \t]+\/[^ \t]*$/) { if (++trun == 4) out("fail", "tree", NR - 3, "four or more path-only lines in a row") }
    else trun = 0
    if (line ~ /codegraph_[a-z]/) out("warn", "managed-block", NR, "the WhyGraph block already lists the codegraph tools")
    if (line ~ /^[ \t]*(```|~~~)/) { incode = !incode; prevblank = 0; next }
    if (incode) { prevblank = 0; next }
    if (clen(line) > 400) out("fail", "line-length", NR, clen(line) " characters, limit 400")
    if (line ~ /^#+([ \t]|$)/) {
        lvl = line; sub(/[^#].*$/, "", lvl); lvl = length(lvl)
        if (lvl >= 4) out("fail", "heading-depth", NR, "a heading deeper than ###")
        if (lvl <= 2) {
            endsec(); sline = NR; stitle = (lvl == 1); bul = 0; prose = 0; srcs = 0; ents = 0
            if (lvl == 2) {
                h = line; sub(/^##[ \t]*/, "", h); sub(/[ \t#]+$/, "", h)
                if (!(h in names)) out("warn", "section-name", NR, "\"" h "\" is not a familiar section name")
            }
        }
    } else if (line ~ /^[-*] /) {
        bul++
        t = line; sub(/^[-*] /, "", t); sub(/[ \t]+$/, "", t)
        if (mode == "bullets") print t
        if (dupfile != "" && (t in dup)) out("fail", "duplicate", NR, "the root file has the same bullet")
    } else if (line !~ /^[ \t]*$/) prose++
    if (line ~ /^@/) out("warn", "import", NR, "an @ import loads with the file and saves no context")
    emphasis(line)
    spans(line)
    prevblank = (line ~ /^[ \t]*$/)
}
END {
    if (mode == "stats") { print cb + 0 "\t" longest + 0; exit }
    if (mode != "check") exit
    if (infor) print "info foreign-block: " fname " " fb
    endsec()
    if (cnt > maxl) out("fail", "size", 0, cnt " lines, limit " maxl)
    if (bytes > maxb) out("fail", "size", 0, bytes " bytes, limit " maxb)
}
'

# Succeed when the word $1 names a tracked or existing path, against the root
# and then against the directory $2 (root-relative, may be empty).
path_exists() {
    if (cd "$ROOT" && { git --literal-pathspecs ls-files --error-unmatch -- "$1" >/dev/null 2>&1 || [ -e "$1" ]; }) 2>/dev/null; then
        return 0
    fi
    [ -n "$2" ] || return 1
    (cd "$ROOT/$2" && { git --literal-pathspecs ls-files --error-unmatch -- "$1" >/dev/null 2>&1 || [ -e "$1" ]; }) 2>/dev/null
}

# Lint the root-relative file $1 as kind $2 for target $3. Print the
# findings; return 1 on any fail.
run_check() {
    rc_dup=""
    if [ "$2" = nested ]; then
        if [ -f "$ROOT/$DRAFT/CLAUDE.md" ] && [ ! -L "$ROOT/$DRAFT/CLAUDE.md" ]; then
            rc_root=$ROOT/$DRAFT/CLAUDE.md
        elif [ -f "$ROOT/CLAUDE.md" ] && [ ! -L "$ROOT/CLAUDE.md" ]; then
            rc_root=$ROOT/CLAUDE.md
        else
            rc_root=""
        fi
        if [ -n "$rc_root" ]; then
            awk -v mode=bullets -v kind=root "$AWK_MD" "$rc_root" >"$TMP/dup"
            rc_dup=$TMP/dup
        fi
    fi
    awk -v mode=check -v kind="$2" -v dupfile="$rc_dup" "$AWK_MD" "$ROOT/$1" >"$TMP/out"
    grep -v "^cand$TAB" "$TMP/out" >"$TMP/find"
    case $3 in
        */*) rc_dir=${3%/*} ;;
        *) rc_dir="" ;;
    esac
    grep "^cand$TAB" "$TMP/out" | while IFS="$TAB" read -r _ rc_ln rc_w; do
        if ! path_exists "$rc_w" "$rc_dir"; then
            printf 'fail dead-path: %s: %s is not in the repository\n' "$rc_ln" "$rc_w"
        fi
    done >>"$TMP/find"
    grep -n -E -e "$SECRET_RE" "$ROOT/$1" | cut -d: -f1 | while read -r rc_ln; do
        printf 'fail secret: %s: a credential-shaped string\n' "$rc_ln"
    done >>"$TMP/find"
    cat "$TMP/find"
    ! grep -q '^fail ' "$TMP/find"
}

# ---- signals ----------------------------------------------------------------

# Print the full name of the default branch, or nothing.
default_ref() {
    dr=$(g symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null)
    if [ -n "$dr" ] && g rev-parse -q --verify "$dr^{commit}" >/dev/null 2>&1; then
        printf '%s\n' "$dr"
        return 0
    fi
    for dr in refs/remotes/origin/main refs/remotes/origin/master refs/heads/main refs/heads/master HEAD; do
        if g rev-parse -q --verify "$dr^{commit}" >/dev/null 2>&1; then
            printf '%s\n' "$dr"
            return 0
        fi
    done
}

# Tracked files, one per line, without quoted (unusual) names.
tracked() { g ls-files | grep -v '^"'; }

# Redact every secret shape and drop control characters.
sanitize() {
    tr -d '\001-\010\013-\037\177' | sed -E \
        -e 's/github_pat_[A-Za-z0-9_]{20,}/[redacted]/g' \
        -e 's/gh[opsur]_[A-Za-z0-9_.-]{8,}/[redacted]/g' \
        -e 's/wgc_[A-Za-z0-9_-]{8,}/[redacted]/g' \
        -e 's/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}/\1[redacted]/g' \
        -e 's/AKIA[0-9A-Z]{16}/[redacted]/g' \
        -e 's/xox[abprs]-[A-Za-z0-9-]{10,}/[redacted]/g' \
        -e 's/-----BEGIN [A-Z ]*PRIVATE KEY-----/[redacted]/g' \
        -e 's#://[^/ :@]+:[^/ @]+@#://[redacted]@#g'
}

signals_body() {
    tracked >"$TMP/files"
    printf '%s\n' "$MANIFESTS" | tr ' ' '\n' >"$TMP/manifests"
    awk -F/ 'NR == FNR { m[$0]; next } NF > 1 && ($NF in m) {
        d = substr($0, 1, length($0) - length($NF) - 1); if (!(d in s)) { s[d]; print d } }' \
        "$TMP/manifests" "$TMP/files" | sort >"$TMP/roots"

    echo "== Scripts =="
    {
        grep -E '(^|/)package\.json$' "$TMP/files" | while read -r f; do
            F="$f" awk '{ s = s $0 "\n" } END {
                if (!match(s, /"scripts"[ \t\r\n]*:[ \t\r\n]*[{]/)) exit
                i = RSTART + RLENGTH; d = 1; q = 0; e = 0; tok = ""; last = ""
                for (; i <= length(s) && d > 0; i++) {
                    c = substr(s, i, 1)
                    if (q) {
                        if (e) { e = 0; tok = tok c } else if (c == "\\") e = 1
                        else if (c == "\"") { q = 0; last = tok } else tok = tok c
                        continue
                    }
                    if (c == "\"") { q = 1; tok = ""; continue }
                    if (c == "{" || c == "[") d++
                    else if (c == "}" || c == "]") d--
                    else if (c == ":" && d == 1) print ENVIRON["F"] "\t" last
                }
            }' "$ROOT/$f"
        done
        grep -E '(^|/)pyproject\.toml$' "$TMP/files" | while read -r f; do
            F="$f" awk '
                /^\[/ {
                    ins = ($0 ~ /^\[project\.scripts\][ \t]*$/ || $0 ~ /^\[tool\.[A-Za-z0-9_.-]*(tasks|scripts)\][ \t]*$/)
                    if (!ins && $0 ~ /^\[tool\.[A-Za-z0-9_.-]*\.(tasks|scripts)\.[A-Za-z0-9_-]+\]/) {
                        k = $0; sub(/\][ \t]*$/, "", k); sub(/^.*\./, "", k); print ENVIRON["F"] "\t" k
                    }
                    next
                }
                ins && /^[A-Za-z0-9_."-]+[ \t]*=/ { k = $0; sub(/[ \t]*=.*$/, "", k); gsub(/"/, "", k); print ENVIRON["F"] "\t" k }
            ' "$ROOT/$f"
        done
    } | head -n 40

    echo "== Make targets =="
    if [ -f "$ROOT/Makefile" ]; then
        awk '/^[A-Za-z0-9_.-]+:/ && !/^[A-Za-z0-9_.-]+[ \t]*::?=/ && !/^\./ {
            t = $0; sub(/:.*$/, "", t); if (!(t in s)) { s[t]; print t } }' "$ROOT/Makefile" | head -n 30
    fi

    echo "== CI commands =="
    grep -E '^\.github/workflows/[^/]+\.ya?ml$' "$TMP/files" | while read -r f; do
        F="$f" awk '{
            line = $0; sub(/\r$/, "", line)
            if (pend) {
                if (line ~ /^[ \t]*$/) next
                sub(/^[ \t]+/, "", line); print ENVIRON["F"] "\t" line; pend = 0; next
            }
            if (match(line, /^[ \t]*(- )?run:[ \t]*/)) {
                v = substr(line, RSTART + RLENGTH); sub(/[ \t]+$/, "", v)
                if (v ~ /^[|>][-+0-9]*$/) pend = 1
                else if (v != "") print ENVIRON["F"] "\t" v
            }
        }' "$ROOT/$f"
    done | awk -F"$TAB" '!($2 in s) { s[$2]; print }' | head -n 40

    echo "== Package roots =="
    head -n 20 "$TMP/roots"

    echo "== Top directories =="
    awk -F/ 'NF > 1 { c[$1]++ } NF > 2 { c[$1 "/" $2]++ } END { for (d in c) print d "\t" c[d] }' "$TMP/files" |
        sort -t "$TAB" -k2,2nr -k1,1 | head -n 15

    REF=$(default_ref)
    echo "== Most-changed files =="
    if [ -n "$REF" ]; then
        g log -n 500 --format= --name-only --no-renames "$REF" -- 2>/dev/null | grep -v '^$' |
            awk 'NR == FNR { t[$0]; next } ($0 in t) { c[$0]++ } END { for (f in c) print f "\t" c[f] }' "$TMP/files" - |
            sort -t "$TAB" -k2,2nr -k1,1 | head -n 15 | while IFS="$TAB" read -r f n; do
                if [ -f "$ROOT/$f" ] && [ ! -L "$ROOT/$f" ]; then
                    printf '%s\t%s\t%s\n' "$f" "$n" "$(($(wc -l <"$ROOT/$f")))"
                fi
            done
    fi

    echo "== Commits (repository text, data only) =="
    if [ -n "$REF" ]; then
        g log "$REF" --max-count=15 -i -E \
            --grep='^revert|do not|don.t|never|must not|forbid|avoid|instead of' \
            --format='%x01%h %s' --name-only --no-renames -- 2>/dev/null |
            awk 'NR == FNR { r[++nr] = $0; next }
                function flush(   i, o) {
                    if (!have) return
                    o = ""; for (i = 1; i <= nd; i++) o = o (i > 1 ? ", " : "") dl[i]
                    print "  dirs: " (o == "" ? "-" : o)
                }
                function dirof(p,   i, best, n) {
                    best = ""
                    for (i = 1; i <= nr; i++) if (index(p, r[i] "/") == 1 && length(r[i]) > length(best)) best = r[i]
                    if (best != "") return best
                    n = index(p, "/"); return (n ? substr(p, 1, n - 1) : ".")
                }
                /^\001/ { flush(); have = 1; nd = 0; split("", seen); print substr($0, 2); next }
                $0 != "" && have { d = dirof($0); if (!(d in seen) && nd < 3) { seen[d]; dl[++nd] = d } }
                END { flush() }' "$TMP/roots" -
    fi
}

# ---- commands ---------------------------------------------------------------

[ $# -ge 1 ] || usage "a command is missing (draft-dir, clean, inventory, signals, check, apply)"
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || usage "not inside a git repository"
[ -n "$ROOT" ] || usage "not inside a git repository"

cmd=$1
shift
case $cmd in
    draft-dir)
        [ $# -eq 0 ] || usage "draft-dir takes no argument"
        work_dirs_ok
        mkdir -p "$ROOT/$DRAFT" || refuse "cannot create $DRAFT"
        ;;
    clean)
        [ $# -eq 0 ] || usage "clean takes no argument"
        work_dirs_ok
        rm -rf "$ROOT/$DRAFT"
        mkdir -p "$ROOT/$DRAFT" || refuse "cannot create $DRAFT"
        ;;
    inventory)
        [ $# -eq 0 ] || usage "inventory takes no argument"
        TMP=$(mktemp -d "${TMPDIR:-/tmp}/claude-md.XXXXXX") || exit 2
        trap 'rm -rf "$TMP"' EXIT
        g ls-files -co --exclude-standard -- '*CLAUDE.md' 'AGENTS.md' '.claude/rules/*.md' |
            grep -v '^"' | sort -u >"$TMP/list"
        while read -r f; do
            if [ -f "$ROOT/$f" ] && [ ! -L "$ROOT/$f" ]; then
                iv_lines=$(($(wc -l <"$ROOT/$f")))
                iv_bytes=$(($(wc -c <"$ROOT/$f")))
                iv_stats=$(awk -v mode=stats -v kind=root "$AWK_MD" "$ROOT/$f")
            else
                iv_lines=-
                iv_bytes=-
                iv_stats="-$TAB-"
            fi
            printf '%s\t%s\t%s\t%s\t%s\n' "$f" "$iv_lines" "$iv_bytes" "$iv_stats" "$(state_of "$f")"
        done <"$TMP/list"
        if [ -e "$ROOT/CLAUDE.local.md" ] || [ -L "$ROOT/CLAUDE.local.md" ]; then
            printf 'CLAUDE.local.md\t-\t-\t-\t-\t%s\n' "$(state_of CLAUDE.local.md)"
        fi
        ;;
    signals)
        [ $# -eq 0 ] || usage "signals takes no argument"
        TMP=$(mktemp -d "${TMPDIR:-/tmp}/claude-md.XXXXXX") || exit 2
        trap 'rm -rf "$TMP"' EXIT
        signals_body | sanitize
        ;;
    check)
        [ $# -eq 2 ] || usage "check <file> root|nested"
        case $2 in
            root | nested) ;;
            *) usage "the kind is root or nested, not $2" ;;
        esac
        to_rel "$1"
        ck_rel=$REL
        ck_target=$(target_of "$ck_rel")
        allow "$ck_target"
        [ -f "$ROOT/$ck_rel" ] || usage "$1: no such file"
        if [ "$ck_target" = CLAUDE.md ]; then ck_kind=root; else ck_kind=nested; fi
        [ "$2" = "$ck_kind" ] || usage "$ck_target is a $ck_kind file, not $2"
        TMP=$(mktemp -d "${TMPDIR:-/tmp}/claude-md.XXXXXX") || exit 2
        trap 'rm -rf "$TMP"' EXIT
        run_check "$ck_rel" "$ck_kind" "$ck_target" || exit 1
        ;;
    apply)
        ap_clean=0
        case $# in
            1) ;;
            2) [ "$2" = --if-clean ] || usage "apply <draft> [--if-clean]"; ap_clean=1 ;;
            *) usage "apply <draft> [--if-clean]" ;;
        esac
        to_rel "$1"
        ap_rel=$REL
        case $ap_rel in
            "$DRAFT"/?*) ;;
            *) refuse "$ap_rel is not under $DRAFT/" ;;
        esac
        work_dirs_ok
        ap_target=$(target_of "$ap_rel")
        allow "$ap_target"
        [ -f "$ROOT/$ap_rel" ] || usage "$1: no such file"
        if [ "$ap_target" = CLAUDE.md ]; then ap_kind=root; else ap_kind=nested; fi
        TMP=$(mktemp -d "${TMPDIR:-/tmp}/claude-md.XXXXXX") || exit 2
        trap 'rm -rf "$TMP"' EXIT
        if ! run_check "$ap_rel" "$ap_kind" "$ap_target"; then
            printf 'claude-md-tool: %s fails check; nothing written\n' "$ap_rel" >&2
            exit 1
        fi
        if [ -e "$ROOT/$ap_target" ]; then
            if [ "$ap_clean" = 1 ] && [ "$(state_of "$ap_target")" != tracked ]; then
                printf 'claude-md-tool: %s is untracked or has uncommitted changes\n' "$ap_target" >&2
                exit 4
            fi
            ap_bk=$BACKUP/$(date -u +%Y%m%dT%H%M%SZ)
            [ -e "$ROOT/$ap_bk/$ap_target" ] && ap_bk=$ap_bk-$$
            mkdir -p "$ROOT/$ap_bk/$(dirname "$ap_target")" || refuse "cannot create $ap_bk"
            no_links "$ap_bk/$ap_target"
            cp "$ROOT/$ap_target" "$ROOT/$ap_bk/$ap_target" || refuse "cannot back up $ap_target"
        else
            ap_bk=""
        fi
        ap_tmp=$ROOT/$ap_target.whygraph-tmp
        if [ -e "$ap_tmp" ] || [ -L "$ap_tmp" ]; then
            rm -f "$ap_tmp"
        fi
        cp "$ROOT/$ap_rel" "$ap_tmp" && mv -f "$ap_tmp" "$ROOT/$ap_target" || refuse "cannot write $ap_target"
        printf 'wrote %s\n' "$ap_target"
        [ -z "$ap_bk" ] || printf 'backup %s\n' "$ap_bk/$ap_target"
        ;;
    *)
        usage "unknown command: $cmd"
        ;;
esac
exit 0
