#!/bin/sh
# plan-tool.sh - the deterministic parts of the WhyGraph planning harness.
#
#   snapshot                    print the tree id of the working tree
#   stem <plan>                 print the plan's stem
#   fingerprint '<p1; p2>'      print the fingerprint of the listed files
#   check <plan>                lint a plan; 0 clean, 1 findings
#   verify <plan> <N>           run step N's literal Verify and record it
#
# Usage errors exit 64 (EX_USAGE).
#
# POSIX sh and POSIX awk only: this must behave the same under dash and bash
# with BSD awk, mawk and gawk. So the awk code has no interval expressions,
# no character classes, no delete of a whole array and no gawk functions; awk
# reads plans from stdin, because an operand holding "=" is an assignment; and
# only awk and tr run with LC_ALL=C, which makes them work on bytes. Nothing
# exports it, because a Verify command must see the environment of the caller.

set -u

usage() {
    [ $# -eq 0 ] || printf 'plan-tool.sh: %s\n' "$1" >&2
    cat >&2 <<'EOF'
usage: plan-tool.sh snapshot
       plan-tool.sh stem <plan>
       plan-tool.sh fingerprint '<path>; <path>'
       plan-tool.sh check <plan>
       plan-tool.sh verify <plan> <N>
EOF
    exit 64
}

# The Verify parser, shared by check and verify. Inside these single-quoted
# awk programs, comments must not hold an apostrophe.
AWK_SPANS='
# Fill SPAN[1..] with the backticked spans of text, and set OUTSIDE to the text
# around them. Returns the span count, or -1 on an unclosed backtick.
function spans_of(text,    at, count) {
    split("", SPAN)
    OUTSIDE = ""
    count = 0
    while ((at = index(text, "`")) > 0) {
        OUTSIDE = OUTSIDE substr(text, 1, at - 1) " "
        text = substr(text, at + 1)
        at = index(text, "`")
        if (at == 0)
            return -1
        SPAN[++count] = substr(text, 1, at - 1)
        text = substr(text, at + 1)
    }
    OUTSIDE = OUTSIDE text
    return count
}

# The number of commands when the Verify text is literal, else 0. Literal means
# only joiners remain after the spans are removed, and no span is blank.
function literal_spans(text,    count, i, n, words) {
    count = spans_of(text)
    if (count <= 0)
        return 0
    for (i = 1; i <= count; i++)
        if (SPAN[i] !~ /[^ \t]/)
            return 0
    text = OUTSIDE
    gsub(/&&|,/, " ", text)
    n = split(text, words, " ")
    for (i = 1; i <= n; i++)
        if (words[i] != "and" && words[i] != "then")
            return 0
    return count
}
'

AWK_VERIFY='
index($0, N ". **") == 1 && !in_block && !found {
    in_block = 1
    found = 1
    next
}
in_block && ($0 ~ /^[0-9]+\. \*\*/ || $0 ~ /^## /) {
    exit
}
in_block && $0 ~ /^[ \t]*(-[ \t]+)?\*\*Verify:\*\*/ {
    sub(/^[ \t]*(-[ \t]+)?\*\*Verify:\*\*/, "")
    count = literal_spans($0)
    for (i = 1; i <= count; i++)
        print SPAN[i]
    exit
}
END {
    # 3 tells the shell that the plan has no such step.
    exit found ? 0 : 3
}
'

AWK_CHECK='
BEGIN {
    # UTF-8 bytes of U+2014 and U+2013; awk runs in the C locale.
    EM_DASH = "\342\200\224"
    EN_DASH = "\342\200\223"
    # The STE sentence limit and the cap on questions that shape the plan.
    MAX_WORDS = 20
    MAX_PLAN_SHAPING = 3
    ROUTE_RE = "^/[A-Za-z0-9._~/%-]*$"
    # A token of only these characters is not a word: "x - y" has two words.
    PUNCTUATION_ONLY = "^[-/+=&|*.?!;,:]+$"

    STRUCTURE_COUNT = split("# Plan:,**Status:**,**Objective:**,**Non-goals:**,## Steps,## Acceptance,## Confidence:", STRUCTURE, ",")
    REQUIRED_COUNT = split("Files,Change,Boundaries,Constraints to preserve,Risks,Test first,Model,Verify", REQUIRED, ",")
    for (i = 1; i <= REQUIRED_COUNT; i++)
        KNOWN_FIELD[REQUIRED[i]] = 1
    KNOWN_FIELD["Routes"] = 1
    KNOWN_FIELD["Expected UI"] = 1
    n = split("haiku,sonnet,opus,fable", words, ",")
    for (i = 1; i <= n; i++)
        MODEL[words[i]] = 1
    PHRASE_COUNT = split(" and then ; and also ;, then ", PHRASE, ";")
    QUOTED_FIELD["Constraints to preserve"] = 1
    QUOTED_FIELD["Risks"] = 1

    section = ""
    field = ""
}

function finding(at, message) {
    printf "line %d: %s\n", at, message
    any_finding = 1
}

function ste_flush() {
    if (sentence_words > MAX_WORDS)
        finding(sentence_at, "sentence over " MAX_WORDS " words (" sentence_words ")")
    sentence_words = 0
}

# Feed one line of STE text. A sentence may run over several lines, so the
# word count stays open until a terminator or a flush.
function ste_feed(text, at,    i, n, parts, phrase) {
    gsub(/`[^`]*`/, "x", text)
    for (i = 1; i <= PHRASE_COUNT; i++) {
        if (index(text " ", PHRASE[i]) > 0) {
            phrase = PHRASE[i]
            gsub(/^[ ,]+| +$/, "", phrase)
            finding(at, "more than one action in a sentence: \"" phrase "\"")
        }
    }
    n = split(text, parts, " ")
    for (i = 1; i <= n; i++) {
        if (parts[i] !~ PUNCTUATION_ONLY) {
            if (sentence_words == 0)
                sentence_at = at
            sentence_words++
        }
        if (parts[i] ~ /[.?!;]$/)
            ste_flush()
    }
}

# The name of the known step field that starts this line, or "".
function field_of(s,    name) {
    if (s !~ /^[ \t]*(-[ \t]+)?\*\*[A-Z][A-Za-z -]*:\*\*/)
        return ""
    name = s
    sub(/^[ \t]*(-[ \t]+)?\*\*/, "", name)
    sub(/:\*\*.*$/, "", name)
    return (name in KNOWN_FIELD) ? name : ""
}

function close_step(    i) {
    ste_flush()
    if (in_step) {
        for (i = 1; i <= REQUIRED_COUNT; i++)
            if (!(REQUIRED[i] in seen))
                finding(step_at, "step " step_no ": missing " REQUIRED[i])
    }
    in_step = 0
    field = ""
}

function close_item() {
    ste_flush()
    if (in_item && !item_has_check)
        finding(item_at, "acceptance item has no **Check:**")
    in_item = 0
}

function close_question() {
    ste_flush()
    if (in_question) {
        if (!question_options)
            finding(question_at, "question has no Options")
        if (!question_default)
            finding(question_at, "question has no Default")
    }
    in_question = 0
}

function check_field(name, value, at,    n, words, word, i) {
    if (name == "Model") {
        n = split(value, words, " ")
        word = (n > 0) ? tolower(words[1]) : ""
        if (!(word in MODEL))
            finding(at, "Model first word is not haiku, sonnet, opus or fable")
    } else if (name == "Verify") {
        if (value !~ /^[ \t]*manual:/ && literal_spans(value) == 0)
            finding(at, "Verify is neither literal nor manual:")
    } else if (name == "Routes") {
        n = spans_of(value)
        if (n <= 0)
            finding(at, "Routes has no backticked path")
        for (i = 1; i <= n; i++)
            if (SPAN[i] !~ ROUTE_RE)
                finding(at, "Routes path fails the route pattern: " SPAN[i])
    } else if (name == "Change" || name == "Expected UI") {
        ste_feed(value, at)
    }
}

function on_step_line(s,    name, value) {
    if (s ~ /^[0-9]+\. \*\*/) {
        close_step()
        in_step = 1
        step_at = NR
        step_no = substr(s, 1, index(s, ".") - 1)
        split("", seen)
        return
    }
    if (!in_step)
        return
    name = field_of(s)
    if (name == "") {
        if (field == "Change" || field == "Expected UI") {
            # A blank line or a list item starts a new sentence.
            if (s ~ /^[ \t]*$/ || s ~ /^[ \t]*([-*+]|[0-9]+\.)[ \t]/)
                ste_flush()
            ste_feed(s, NR)
        }
        return
    }
    ste_flush()
    field = name
    seen[name] = 1
    value = s
    sub(/^[ \t]*(-[ \t]+)?\*\*[^:]*:\*\*/, "", value)
    check_field(name, value, NR)
}

function on_acceptance_line(s,    at) {
    if (s ~ /^[0-9]+\. /) {
        close_item()
        in_item = 1
        item_at = NR
        item_has_check = 0
    }
    if (!in_item || item_has_check)
        return
    at = index(s, "**Check:**")
    if (at > 0) {
        item_has_check = 1
        ste_feed(substr(s, 1, at - 1), NR)
        ste_flush()
    } else {
        ste_feed(s, NR)
    }
}

function on_question_line(s,    at) {
    if (s ~ /^- /) {
        close_question()
        in_question = 1
        question_at = NR
        question_options = 0
        question_default = 0
        question_counted = 0
        question_text = 1
        sub(/^- \*\*Q[0-9]+\.\*\*/, "", s)
    }
    if (!in_question)
        return
    if (index(s, "Options:") > 0)
        question_options = 1
    if (index(s, "Default:") > 0)
        question_default = 1
    if (!question_counted && s ~ /Plan-shaping:(\*\*)?[ \t]*yes/) {
        question_counted = 1
        if (++plan_shaping == MAX_PLAN_SHAPING + 1)
            finding(NR, "more than " MAX_PLAN_SHAPING " questions have Plan-shaping: yes")
    }
    if (!question_text)
        return
    # Only the question is prose: the labelled fields are a fixed format.
    at = index(s, "**Options:**")
    if (at > 0) {
        question_text = 0
        ste_feed(substr(s, 1, at - 1), NR)
        ste_flush()
    } else {
        ste_feed(s, NR)
    }
}

{
    line = $0
    if (line ~ /^[ \t]*(```|~~~)/) {
        in_fence = !in_fence
        next
    }
    if (in_fence)
        next

    if (line ~ /^# Plan:/)
        have["# Plan:"] = 1
    else if (line ~ /^\*\*Status:\*\*/)
        have["**Status:**"] = 1
    else if (line ~ /^\*\*Objective:\*\*/)
        have["**Objective:**"] = 1
    else if (line ~ /^\*\*Non-goals:\*\*/)
        have["**Non-goals:**"] = 1

    if (line ~ /^## /) {
        close_step()
        close_item()
        close_question()
        section = "other"
        if (line ~ /^## Steps[ \t]*$/) {
            section = "steps"
            have["## Steps"] = 1
        } else if (line ~ /^## Acceptance[ \t]*$/) {
            section = "acceptance"
            have["## Acceptance"] = 1
        } else if (line ~ /^## Open questions/) {
            section = "questions"
        } else if (line ~ /^## Risks called out/) {
            section = "risks"
        } else if (line ~ /^## Confidence:/) {
            have["## Confidence:"] = 1
        }
    } else if (section == "steps") {
        on_step_line(line)
    } else if (section == "acceptance") {
        on_acceptance_line(line)
    } else if (section == "questions") {
        on_question_line(line)
    }

    # Verbatim quotes are exempt from the dash rule: the constraint and risk
    # fields and section, blockquotes, double-quoted text and code spans.
    if (section == "risks" || field in QUOTED_FIELD)
        next
    if (line ~ /\*\*Constraint at risk:\*\*/ || line ~ /^[ \t]*>/)
        next
    gsub(/`[^`]*`/, "", line)
    gsub(/"[^"]*"/, "", line)
    if (index(line, EM_DASH) > 0)
        finding(NR, "em dash outside a quote")
    if (index(line, EN_DASH) > 0)
        finding(NR, "en dash outside a quote")
}

END {
    close_step()
    close_item()
    close_question()
    for (i = 1; i <= STRUCTURE_COUNT; i++)
        if (!(STRUCTURE[i] in have))
            finding(1, "missing " STRUCTURE[i])
    exit any_finding ? 1 : 0
}
'

# The unchanged snapshot command of the implementor: a throwaway index seeded
# from the real one, so neither the real index nor the stash list changes.
take_snapshot() {
    sh -c 'i="$(mktemp -u)"; cp "$(git rev-parse --git-path index)" "$i" 2>/dev/null; GIT_INDEX_FILE="$i" git add --all && GIT_INDEX_FILE="$i" git write-tree; s=$?; rm -f "$i"; exit $s'
}

stem_of() {
    stem=${1##*/}
    stem=${stem%.md}
    stem=$(printf '%s' "$stem" | LC_ALL=C tr 'A-Z' 'a-z' | LC_ALL=C tr -c 'a-z0-9' '-' | LC_ALL=C tr -s '-')
    stem=${stem#-}
    stem=${stem%-}
    printf '%s\n' "$stem"
}

# The characters are spelled out because a range such as a-z follows the
# collation order of the locale in some shells.
path_is_safe() {
    case $1 in
        /* | -*) return 1 ;;
        *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/-]*) return 1 ;;
    esac
    case /$1/ in
        */../*) return 1 ;;
    esac
    return 0
}

cmd_fingerprint() {
    [ $# -eq 1 ] || usage
    # The paths are repo-relative. Outside a repo, hash-object still works.
    if root=$(git rev-parse --show-toplevel 2>/dev/null); then
        cd "$root" || exit 1
    fi
    separator='; '
    rest=$1
    while :; do
        case $rest in
            *"$separator"*)
                path=${rest%%"$separator"*}
                rest=${rest#*"$separator"}
                more=1
                ;;
            *)
                path=$rest
                more=
                ;;
        esac
        hash=missing
        if path_is_safe "$path"; then
            hash=$(git hash-object "$path" 2>/dev/null) || hash=missing
        fi
        printf '%s %s\n' "$hash" "$path"
        [ -n "$more" ] || break
    done | git hash-object --stdin
}

cmd_check() {
    [ $# -eq 1 ] || usage
    [ -f "$1" ] || usage "no such plan: $1"
    LC_ALL=C awk "$AWK_SPANS$AWK_CHECK" <"$1"
}

cmd_verify() {
    [ $# -eq 2 ] || usage
    plan=$1
    step=$2
    case $step in
        '' | 0* | *[!0123456789]*) usage "not a step number: $step" ;;
    esac
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    LC_ALL=C awk -v N="$step" "$AWK_SPANS$AWK_VERIFY" <"$plan" >"$work/spans"
    [ $? -ne 3 ] || usage "no step $step in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    steps_dir=.whygraph/plans/$stem/steps
    record=$steps_dir/$step.verify

    if [ ! -s "$work/spans" ]; then
        mkdir -p "$steps_dir"
        printf 'exit: not literal\n' >"$record"
        printf 'verify: not literal\n'
        exit 0
    fi

    printf 'verify: literal\n'
    code=0
    : >"$work/ran"
    : >"$work/output"
    while IFS= read -r span; do
        printf 'command: %s\n' "$span" >>"$work/ran"
        # stdin is closed so that a command cannot eat the list of spans.
        sh -c "$span" >>"$work/output" 2>&1 </dev/null
        code=$?
        [ "$code" -eq 0 ] || break
    done <"$work/spans"

    # "none" can never equal a 40-hex end tree, so the evaluator escalates.
    tree=$(take_snapshot) || tree=none
    mkdir -p "$steps_dir"
    {
        cat "$work/ran"
        printf 'exit: %s\n' "$code"
        printf 'tree: %s\n' "$tree"
        printf '%s\n' '--- last 40 lines of output'
        tail -n 40 "$work/output"
    } >"$record"
    tail -n 40 "$work/output"
    exit "$code"
}

[ $# -ge 1 ] || usage
subcommand=$1
shift
case $subcommand in
    snapshot)
        [ $# -eq 0 ] || usage
        take_snapshot
        ;;
    stem)
        [ $# -eq 1 ] || usage
        stem_of "$1"
        ;;
    fingerprint) cmd_fingerprint "$@" ;;
    check) cmd_check "$@" ;;
    verify) cmd_verify "$@" ;;
    *) usage "unknown subcommand: $subcommand" ;;
esac
