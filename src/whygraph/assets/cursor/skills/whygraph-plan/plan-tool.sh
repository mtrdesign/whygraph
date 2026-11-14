#!/bin/sh
# plan-tool.sh - the deterministic parts of the WhyGraph planning harness.
#
#   snapshot                    print the tree id of the working tree
#   stem <plan>                 print the plan's stem
#   fingerprint '<p1; p2>'      print the fingerprint of the listed files
#   check <plan>                lint a plan; 0 clean, 1 findings
#   verify <plan> <N>           run step N's literal Verify and record it
#   begin <plan> <N>            start step N; print its start tree
#   end <plan> <N> <t0> [<blob>]
#                               run step N's Verify; write its end tree and diff
#   pass <plan> <N>             mark step N done (after the command's gates)
#   next <plan>                 print the first step not done, or "done"
#   changed <before> <after>    print the paths that differ; 1 when any does
#   probe <plan>                print every Verify and Check command; list
#                               the commands that are missing
#   baseline <plan> <N> [first] [<blob>]
#                               run step N's Verify before any edit; append
#                               the result to the baseline file
#   accept <plan> <i> [<blob>]  run Acceptance item i's Check and record it
#   attempt <plan> <N> <kind> [<cap> | <detail>]
#                               count a recovery run of step N; 1 at the cap
#   amends <plan> <N> [<blob>]  1 when step N or the plan has no amendment left
#   save <plan>                 store the plan as a blob; print its id
#   restore <plan> <blob>       put the saved plan back
#   log-check <plan> <blob>     1 when the plan changed outside its log; else
#                               store it and print the new blob id
#   state <plan> [<hash>]       print the hash of the files no agent may
#                               change; with <hash>, 1 when it differs
#   amend-check <plan> <blob> <N>
#                               check the amendment of step N; on a pass,
#                               write its "## Amendments" entry and print
#                               the new blob id
#   constraints <plan> [<blob>] [<N>]
#                               search for each line of "## Constraint
#                               checks"; 1 when one is broken
#   brief <plan>                print a page about the plan for a person
#
# Usage errors exit 64 (EX_USAGE). The plan's files live in
# .whygraph/plans/<stem>/steps/. A subcommand that takes a plan exits 77 when
# git tracks a file under .whygraph/plans/<stem>/, or when .whygraph,
# .whygraph/plans, a directory under the stem or a file it writes is a
# symlink. A whole file is written to a temporary file in steps/, then moved
# into place. Every git call ignores fsmonitor, hooks and replace refs. A
# snapshot or diff that fails exits 70.
#
# end exits 0 when Verify passes or is not literal, 1 when it fails, 4 when
# it exits 126 or 127 and its first word is not a command, and 124 when the
# budget ran out. It exits 65 when the step changed a copy of this tool; 65
# wins over the others. A <blob> that does not hash to its id exits 65.
#
# probe runs no command. It exits 4 when the first word of a command, or a
# script that sh or bash would read, is missing. baseline exits 0 for any
# result it records, 3 when the budget ran out and 4 when Verify exited 126
# or 127 and its first word is not a command. It never writes <N>.verify.
# A pass whose output has a line that holds one of the phrases of
# AWK_NO_TEST is recorded as "pass (no test ran)": the first word stays
# "pass", so every reader of the baseline file still sees a pass.
# accept exits 0 when the Check passes or is not literal, 1 when it fails
# and 124 when the budget ran out.
#
# attempt kinds and caps per step: retry <detail> 1, findings <cap> (0 to 2),
# constraint 1; reset takes no cap. A detail holds only a-z, 0-9, ":" and
# "-". A count runs from the last reset line, which counts for every step;
# reset never removes a line. At the cap, attempt appends nothing. Amendments
# are counted from the plan's "## Amendments" section and never reset: one
# per step, three per plan.
#
# Every saved blob is read with replace refs off and must hash back to its
# id; otherwise exit 65. save, restore, log-check and amend-check refuse a
# symlinked plan (77). log-check ignores the "## Implementation log"
# section, the blank lines before it and at the end. state hashes .git/config,
# .git/hooks/, attempts, baseline and the <N>.done markers. amend-check
# allows one changed line, step N's Verify or Files line, and exits 1 with
# one "amend-check:" line per broken rule: a new Verify must be literal,
# hold no shell syntax and no git push, keep the number of commands, and
# re-point at most one reviewed command at another tracked file or test id;
# a new Files path must be named in the Change and in no Boundaries.
#
# constraints reads the plan, or the saved plan <blob> as end does, and
# runs no command of the plan. For each check line it runs
#   git grep -nIF --untracked -e <text> -- ':(glob)<glob>'
# from the repository root, with the text and the glob as arguments; no
# shell reads them. Tracked and untracked files count, ignored and binary
# ones do not. An absent text with a hit prints "broken: note #<id>
# <path>:<line>" for the first hit; a present text with none prints
# "broken: note #<id> (no match)". With <N>, a note that the Changes on
# purpose field of step N or an earlier step names as "(note #<id>)" is
# skipped. The last line is "constraints: <k> hold, <b> broken". It exits 0
# when none is broken, 1 when one is, 6 when the plan has no "## Constraint
# checks" section, 5 when a line under it breaks the fixed form or the
# character set (check prints the same findings, see AWK_CONSTRAINT), and
# 70 when git grep fails. It writes no file.
#
# brief reads only the plan file: no git, no work directory, no command of
# the plan. It prints the "# Plan:" title, the Objective, then the lines of
# "## Blockers", "## Open questions" and "## Constraint checks", one line
# per step (its name, the number of Files entries, the first word of its
# Model, and whether it has a Changes on purpose field), and the first line
# of each Acceptance item. A part the plan lacks prints nothing. It exits 0.
#
# The plan parser reads step lines only under "## Steps", Acceptance items
# only under "## Acceptance", and skips fenced lines.
#
# verify, end, baseline and accept run their commands under one time budget
# per call: WHYGRAPH_VERIFY_TIMEOUT seconds, default 540, at most 580. A run
# over the budget is killed and recorded as exit 124. The script calls no
# timeout command, because macOS has none.
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
       plan-tool.sh begin <plan> <N>
       plan-tool.sh end <plan> <N> <t0> [<blob>]
       plan-tool.sh pass <plan> <N>
       plan-tool.sh next <plan>
       plan-tool.sh changed <before> <after>
       plan-tool.sh probe <plan>
       plan-tool.sh baseline <plan> <N> [first] [<blob>]
       plan-tool.sh accept <plan> <i> [<blob>]
       plan-tool.sh attempt <plan> <N> retry <detail>
       plan-tool.sh attempt <plan> <N> findings <cap>
       plan-tool.sh attempt <plan> <N> constraint|reset
       plan-tool.sh amends <plan> <N> [<blob>]
       plan-tool.sh save <plan>
       plan-tool.sh restore <plan> <blob>
       plan-tool.sh log-check <plan> <blob>
       plan-tool.sh state <plan> [<hash>]
       plan-tool.sh amend-check <plan> <blob> <N>
       plan-tool.sh constraints <plan> [<blob>] [<N>]
       plan-tool.sh brief <plan>
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

# The shared plan scanner. It reads step lines only under "## Steps",
# Acceptance items only under "## Acceptance", and skips fenced lines, as
# AWK_CHECK does. With N set, it prints field F of step N: the literal spans
# for Verify, else the text after the label, from the label line only. RAW
# "raw" prints a Verify as the text after its label too; RAW "line" prints
# the line number of the label line; RAW "all" adds the lines that follow
# the label line up to the next field or step (a Change or Boundaries
# paragraph). F "Check" prints the literal spans of the Check of Acceptance
# item N instead. With N empty, it prints "step <n>" for each step in order,
# "item <i>" for each Acceptance item, "logged <n>" for each
# "- [x] Step <n>:" line of the "## Implementation log" section, and
# "amended <n>" for each "- Step <n>:" line of the "## Amendments" section
# ("amended ?" for any other "- " line there).
AWK_VERIFY='
# Whether line s starts with the field name; then VALUE is the text after it.
function field_value(s, name) {
    if (!sub(/^[ \t]*(-[ \t]+)?\*\*/, "", s) || index(s, name ":**") != 1)
        return 0
    VALUE = substr(s, length(name) + 4)
    return 1
}

$0 ~ /^[ \t]*(```|~~~)/ {
    in_fence = !in_fence
    next
}
in_fence {
    next
}
/^## / {
    if (in_block)
        exit
    in_steps = ($0 ~ /^## Steps[ \t]*$/)
    in_items = ($0 ~ /^## Acceptance[ \t]*$/)
    in_log = ($0 ~ /^## Implementation log[ \t]*$/)
    in_amend = ($0 ~ /^## Amendments[ \t]*$/)
    next
}
N == "" && in_log && index($0, "- [x] Step ") == 1 {
    number = substr($0, 12)
    sub(/:.*$/, "", number)
    if (number ~ /^[0-9]+$/ && index(substr($0, 12), number ":") == 1)
        print "logged " number
    next
}
N == "" && in_amend && index($0, "- ") == 1 {
    number = substr($0, 8)
    sub(/:.*$/, "", number)
    if (index($0, "- Step ") == 1 && number ~ /^[0-9]+$/ && index(substr($0, 8), number ":") == 1)
        print "amended " number
    else
        print "amended ?"
    next
}
in_items && /^[0-9]+\. / {
    if (in_block)
        exit
    number = substr($0, 1, index($0, ".") - 1)
    if (N == "") {
        print "item " number
    } else if (F == "Check" && number == N) {
        in_block = 1
        found = 1
    }
}
# The Check of an item may sit on its first line or a later one.
in_items && in_block {
    at = index($0, "**Check:**")
    if (at > 0) {
        count = literal_spans(substr($0, at + 10))
        for (i = 1; i <= count; i++)
            print SPAN[i]
        exit
    }
    next
}
!in_steps {
    next
}
/^[0-9]+\. \*\*/ {
    if (in_block)
        exit
    if (N == "") {
        print "step " substr($0, 1, index($0, ".") - 1)
        found = 1
    } else if (F != "Check" && index($0, N ". **") == 1) {
        in_block = 1
        found = 1
    }
    next
}
# The rest of a paragraph field, for RAW "all".
collecting {
    if ($0 ~ /^[ \t]*(-[ \t]+)?\*\*[A-Z][A-Za-z -]*:\*\*/)
        exit
    print
    next
}
in_block && field_value($0, F) {
    if (RAW == "line") {
        print NR
    } else if (F == "Verify" && RAW == "") {
        count = literal_spans(VALUE)
        for (i = 1; i <= count; i++)
            print SPAN[i]
    } else {
        print VALUE
        if (RAW == "all") {
            collecting = 1
            next
        }
    }
    exit
}
END {
    # 3 tells the shell that the plan has no such step or item, or no step.
    exit found ? 0 : 3
}
'

# The paths read from stdin that the Files text in $FILES does not name.
# An entry is the first word of each comma-separated part, after
# parenthesized notes such as (new) and backticks are removed, without a
# :<line> suffix or a leading ./. An entry that ends in / names every path
# under it. With PREFIX set, each printed path follows it. With LIST set, it
# reads nothing and prints each part instead, as "<entry> TAB <word count>
# TAB <words>", with "-" for an empty entry.
AWK_OUTSIDE='
BEGIN {
    text = ENVIRON["FILES"]
    gsub(/\([^)]*\)/, " ", text)
    gsub(/`/, " ", text)
    n = split(text, parts, ",")
    for (i = 1; i <= n; i++) {
        count_words = split(parts[i], words, " ")
        if (count_words == 0)
            continue
        entry = words[1]
        sub(/:[0-9].*$/, "", entry)
        sub(/^\.\//, "", entry)
        if (entry != "")
            ENTRY[++count] = entry
        if (ENVIRON["LIST"] != "") {
            printf "%s\t%d\t%s", (entry == "" ? "-" : entry), count_words, words[1]
            for (k = 2; k <= count_words; k++)
                printf " %s", words[k]
            printf "\n"
        }
    }
    if (ENVIRON["LIST"] != "")
        exit
}
{
    path = $0
    sub(/^\.\//, "", path)
    for (i = 1; i <= count; i++) {
        if (path == ENTRY[i])
            next
        if (substr(ENTRY[i], length(ENTRY[i])) == "/" && index(path, ENTRY[i]) == 1)
            next
    }
    print ENVIRON["PREFIX"] $0
}
'

# The first word of each part of a span, as "command <word>". A span splits
# on &&, ||, ; and | outside single and double quotes. A word skips
# NAME=value words, a leading (, { or ! and a trailing ) or }. When the word
# is sh or bash, the script it reads follows as "script <word>": the first
# word after the options, none with -c.
AWK_FIRST_WORDS='
function bare(word) {
    while (word ~ /^[({!]/)
        word = substr(word, 2)
    while (word ~ /[)}]$/)
        word = substr(word, 1, length(word) - 1)
    return word
}

function first_word(part,    n, words, i, word) {
    n = split(part, words, " ")
    for (i = 1; i <= n; i++) {
        word = bare(words[i])
        if (word == "" || word ~ /^[A-Za-z_][A-Za-z0-9_]*=/)
            continue
        print "command " word
        if (word != "sh" && word != "bash")
            return
        for (i++; i <= n; i++) {
            word = bare(words[i])
            if (word ~ /^-[A-Za-z]*c/)
                return
            if (word == "-o" || word == "+o")
                i++
            else if (word != "" && word !~ /^[-+]/) {
                print "script " word
                return
            }
        }
        return
    }
}

{
    part = ""
    quote = ""
    n = length($0)
    for (i = 1; i <= n; i++) {
        c = substr($0, i, 1)
        if (quote == "\"" && c == "\\" && i < n) {
            part = part c substr($0, ++i, 1)
        } else if (quote != "") {
            if (c == quote)
                quote = ""
            part = part c
        } else if (c == "\"" || c == "\047") {
            quote = c
            part = part c
        } else if (c == ";" || c == "|" || (c == "&" && substr($0, i + 1, 1) == "&")) {
            if (c == "&")
                i++
            first_word(part)
            part = ""
        } else {
            part = part c
        }
    }
    first_word(part)
}
'

# The constraint check line, shared by check and constraints. A line under
# "## Constraint checks" reads
#   - note #<id> - <absent | present>: "<text>" in <glob>
# The text is 3 to 200 characters with no control character, no line or
# paragraph separator, no bidi control, no double quote, no backtick and no
# backslash. The glob is 1 to 200 of A-Z, a-z, 0-9, ".", "_", "-", "/" and
# "*", with no leading "/", no ".." part and at most two "**". These are the
# rules the portal applies to a note check. Bytes from 0x80 up count as one
# character per UTF-8 lead byte.
AWK_CONSTRAINT='
# Read line s as a check. Returns "" and sets C_ID, C_KIND, C_TEXT and C_GLOB,
# or returns "form" or "chars" for the first rule the line breaks.
function constraint_of(s,    rest, at, n, i, c, next_byte, chars, parts, stars) {
    if (!SEPARATORS) {
        # LRM, RLM, LS, PS, LRE to RLO, and LRI to PDI, in UTF-8.
        SEPARATORS = split("\342\200\216 \342\200\217 \342\200\250 \342\200\251 \342\200\252 \342\200\253 \342\200\254 \342\200\255 \342\200\256 \342\201\246 \342\201\247 \342\201\250 \342\201\251", SEPARATOR, " ")
        GLOB_SET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/*"
    }
    sub(/[ \t]+$/, "", s)
    if (s !~ /^- note #[1-9][0-9]* - (absent|present): "/)
        return "form"
    rest = substr(s, 9)
    at = index(rest, " ")
    C_ID = substr(rest, 1, at - 1)
    rest = substr(rest, at + 3)
    at = index(rest, ":")
    C_KIND = substr(rest, 1, at - 1)
    rest = substr(rest, at + 3)
    at = index(rest, "\"")
    if (at == 0)
        return "form"
    C_TEXT = substr(rest, 1, at - 1)
    rest = substr(rest, at + 1)
    if (substr(rest, 1, 4) != " in " || length(rest) == 4)
        return "form"
    C_GLOB = substr(rest, 5)

    n = length(C_TEXT)
    chars = 0
    for (i = 1; i <= n; i++) {
        c = substr(C_TEXT, i, 1)
        if (c < " " || c == "\177" || c == "`" || c == "\\")
            return "chars"
        next_byte = substr(C_TEXT, i + 1, 1)
        # A C1 control: U+0080 to U+009F.
        if (c == "\302" && next_byte >= "\200" && next_byte < "\240")
            return "chars"
        if (c < "\200" || c >= "\300")
            chars++
    }
    for (i = 1; i <= SEPARATORS; i++)
        if (index(C_TEXT, SEPARATOR[i]) > 0)
            return "chars"
    if (chars < 3 || chars > 200)
        return "form"

    n = length(C_GLOB)
    for (i = 1; i <= n; i++)
        if (index(GLOB_SET, substr(C_GLOB, i, 1)) == 0)
            return "chars"
    if (n > 200 || substr(C_GLOB, 1, 1) == "/")
        return "form"
    n = split(C_GLOB, parts, "/")
    for (i = 1; i <= n; i++)
        if (parts[i] == "..")
            return "form"
    stars = 0
    rest = C_GLOB
    while ((at = index(rest, "**")) > 0) {
        stars++
        rest = substr(rest, at + 2)
    }
    return (stars > 2) ? "form" : ""
}

# The finding for what constraint_of returned, or "".
function constraint_finding(broken) {
    if (broken == "form")
        return "Constraint check is not in the fixed form"
    if (broken == "chars")
        return "Constraint check has a character outside the allowed set"
    return ""
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
    KNOWN_FIELD["Changes on purpose"] = 1
    n = split("haiku,sonnet,opus,fable", words, ",")
    for (i = 1; i <= n; i++)
        MODEL[words[i]] = 1
    PHRASE_COUNT = split(" and then ; and also ;, then ", PHRASE, ";")
    QUOTED_FIELD["Constraints to preserve"] = 1
    QUOTED_FIELD["Risks"] = 1
    QUOTED_FIELD["Changes on purpose"] = 1

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
        } else if (line ~ /^## Constraint checks[ \t]*$/) {
            section = "constraints"
        }
    } else if (section == "constraints") {
        if (line ~ /[^ \t]/ && constraint_finding(constraint_of(line)) != "")
            finding(NR, constraint_finding(constraint_of(line)))
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

# The checks of the plan read from stdin, as tab-separated lines: "section"
# for each "## Constraint checks" heading, "check <id> <kind> <glob> <text>"
# for a good line under it, "bad <line number> <finding>" for any other
# non-blank line, and "exempt <id>" for each "(note #<id>)" on the Changes on
# purpose field of step N or an earlier step. With N empty, no line is
# exempt. Fenced lines are skipped.
AWK_CONSTRAINTS='
$0 ~ /^[ \t]*(```|~~~)/ {
    in_fence = !in_fence
    next
}
in_fence {
    next
}
/^## / {
    in_steps = ($0 ~ /^## Steps[ \t]*$/)
    in_checks = ($0 ~ /^## Constraint checks[ \t]*$/)
    if (in_checks)
        print "section"
    step = 0
    purpose = 0
    next
}
in_checks && /[^ \t]/ {
    broken = constraint_finding(constraint_of($0))
    if (broken != "")
        printf "bad\t%d\t%s\n", NR, broken
    else
        printf "check\t%s\t%s\t%s\t%s\n", C_ID, C_KIND, C_GLOB, C_TEXT
    next
}
!in_steps || N == "" {
    next
}
/^[0-9]+\. \*\*/ {
    step = substr($0, 1, index($0, ".") - 1) + 0
    purpose = 0
    next
}
/^[ \t]*(-[ \t]+)?\*\*[A-Z][A-Za-z -]*:\*\*/ {
    name = $0
    sub(/^[ \t]*(-[ \t]+)?\*\*/, "", name)
    sub(/:\*\*.*$/, "", name)
    purpose = (name == "Changes on purpose")
}
purpose && step >= 1 && step <= N + 0 {
    rest = $0
    while (match(rest, /\(note #[0-9]+\)/)) {
        printf "exempt\t%s\n", substr(rest, RSTART + 7, RLENGTH - 8)
        rest = substr(rest, RSTART + RLENGTH)
    }
}
'

# Exit 0 when a line of the Verify output read from stdin holds one of the
# phrases below, matched without case, with no letter or digit right before
# or after it; else 1. So "Ran 0 tests" matches and "10 tests" does not.
AWK_NO_TEST='
BEGIN {
    count = split("no tests ran|collected 0 items|0 passing|running 0 tests|no tests to run|no tests found|0 tests", PHRASE, "|")
}
{
    line = tolower($0)
    for (i = 1; i <= count; i++) {
        at = 0
        while ((k = index(substr(line, at + 1), PHRASE[i])) > 0) {
            at += k
            before = (at > 1) ? substr(line, at - 1, 1) : ""
            after = substr(line, at + length(PHRASE[i]), 1)
            if (before !~ /[a-z0-9]/ && after !~ /[a-z0-9]/) {
                found = 1
                exit
            }
        }
    }
}
END {
    exit found ? 0 : 1
}
'

# The page of brief, from the plan read from stdin. Fences count as in
# AWK_VERIFY. Each group prints only when it has a line.
AWK_BRIEF='
function trimmed(s) {
    sub(/^[ \t]+/, "", s)
    sub(/[ \t]+$/, "", s)
    return s
}
function add(group, s) {
    PAGE[group] = PAGE[group] "  " trimmed(s) "\n"
}
function end_step(    text) {
    if (number == "")
        return
    text = number ". " name " - " files (files == 1 ? " file" : " files")
    text = text ", " (model == "" ? "no model" : model)
    if (purpose)
        text = text ", changes a constraint on purpose"
    add("Steps", text)
    number = ""
}
function show(group) {
    if (PAGE[group] != "")
        printf "%s:\n%s", group, PAGE[group]
}
$0 ~ /^[ \t]*(```|~~~)/ {
    in_fence = !in_fence
    next
}
in_fence {
    next
}
title == "" && index($0, "# Plan: ") == 1 {
    title = trimmed(substr($0, 9))
    next
}
/^## / {
    end_step()
    section = trimmed(substr($0, 4))
    next
}
section == "" && objective == "" && index($0, "**Objective:**") == 1 {
    objective = trimmed(substr($0, 15))
    next
}
(section == "Blockers" || section == "Open questions" || section == "Constraint checks") && index($0, "- ") == 1 {
    add(section, $0)
    next
}
section == "Acceptance" && /^[0-9]+\. / {
    add("Acceptance", $0)
    next
}
section != "Steps" {
    next
}
/^[0-9]+\. \*\*/ {
    end_step()
    number = substr($0, 1, index($0, ".") - 1)
    name = substr($0, index($0, "**") + 2)
    if (index(name, "**") > 0)
        name = substr(name, 1, index(name, "**") - 1)
    name = trimmed(name)
    files = 0
    model = ""
    purpose = 0
    next
}
number == "" || $0 !~ /^[ \t]*(-[ \t]+)?\*\*[A-Z][A-Za-z -]*:\*\*/ {
    next
}
{
    label = $0
    sub(/^[ \t]*(-[ \t]+)?\*\*/, "", label)
    value = substr(label, index(label, ":**") + 3)
    sub(/:\*\*.*$/, "", label)
    if (label == "Files") {
        gsub(/\([^)]*\)/, " ", value)
        n = split(value, part, ",")
        for (i = 1; i <= n; i++)
            if (part[i] ~ /[^ \t`]/)
                files++
    } else if (label == "Model") {
        split(value, word, " ")
        model = tolower(word[1])
    } else if (label == "Changes on purpose") {
        purpose = 1
    }
}
END {
    end_step()
    if (title != "")
        print "Plan: " title
    if (objective != "")
        print "Objective: " objective
    show("Blockers")
    show("Open questions")
    show("Constraint checks")
    show("Steps")
    show("Acceptance")
}
'

# The plan read from stdin without its "## Implementation log" sections,
# as "<line number> TAB <line>". A section runs to the next "## " heading
# or the end of the file, and the blank lines directly before its heading
# go too, as do the blank lines at the end. Fences count as in AWK_VERIFY,
# inside the log as well, so a fence written into the log cannot hide a
# heading: the kept lines of two copies are then never equal.
AWK_STRIP='
{
    line = $0
    is_fence = (line ~ /^[ \t]*(```|~~~)/)
    if (strip) {
        if (is_fence) {
            fence = !fence
            next
        }
        if (fence || line !~ /^## /)
            next
        strip = 0
    }
    if (is_fence) {
        fence = !fence
    } else if (!fence && line ~ /^## Implementation log[ \t]*$/) {
        held = 0
        strip = 1
        next
    }
    if (line !~ /[^ \t]/) {
        HELD[++held] = NR "\t" line
        next
    }
    for (i = 1; i <= held; i++)
        print HELD[i]
    held = 0
    print NR "\t" line
}
'

# Compare the kept lines on stdin with those of the file named by $OLD, as
# AWK_STRIP prints them. Prints the first difference and exits 1.
AWK_SAME='
function text(s) {
    return substr(s, index(s, "\t") + 1)
}
function number(s) {
    return substr(s, 1, index(s, "\t") - 1)
}
BEGIN {
    old = ENVIRON["OLD"]
}
{
    if ((getline line < old) <= 0) {
        print "log-check: line " number($0) " was added outside the log: " text($0)
        bad = 1
        exit
    }
    if (text(line) != text($0)) {
        print "log-check: line " number($0) " changed outside the log: " text($0)
        bad = 1
        exit
    }
}
END {
    if (!bad && (getline line < old) > 0) {
        print "log-check: line " number(line) " of the saved plan was removed: " text(line)
        bad = 1
    }
    exit bad
}
'

# The numbers of the lines of stdin that differ from the file named by
# $NEW, as "line <n>", and "count" when the two have a different number of
# lines.
AWK_LINES='
BEGIN {
    new = ENVIRON["NEW"]
}
{
    if ((getline line < new) > 0) {
        if (line != $0)
            print "line " NR
    } else {
        short = 1
    }
}
END {
    if (short || (getline line < new) > 0)
        print "count"
}
'

# For each command of the file named by $NEW_SPANS (one per line), each
# command read from stdin with the same number of words that differs from
# it only after its program (the first word that is not NAME=value), never
# in a flag, and never between a test id (<file>::<name>) and another word:
# "<i> <old> <new> ...", with the differing pairs. Words split on blanks and
# tabs, so runs of them count as one.
AWK_SWAPS='
BEGIN {
    file = ENVIRON["NEW_SPANS"]
    while ((getline line < file) > 0)
        NEW[++count] = line
}
{
    REVIEWED[++reviewed] = $0
}
END {
    for (i = 1; i <= count; i++) {
        n = split(NEW[i], a, " ")
        for (j = 1; j <= reviewed; j++) {
            if (split(REVIEWED[j], b, " ") != n)
                continue
            pairs = ""
            ok = 1
            program = 1
            while (program < n && b[program] ~ /^[A-Za-z_][A-Za-z0-9_]*=/)
                program++
            for (p = 1; p <= n && ok; p++) {
                if (a[p] == b[p])
                    continue
                if (p <= program || a[p] ~ /^-/ || b[p] ~ /^-/ || (index(a[p], "::") > 0) != (index(b[p], "::") > 0))
                    ok = 0
                else
                    pairs = pairs " " b[p] " " a[p]
            }
            if (ok)
                print i pairs
        }
    }
}
'

# The words of the text on stdin, one per line, each without one trailing
# ".", ",", ";" or ":", then one pair of backticks, then a leading ./.
AWK_TOKENS='
{
    n = split($0, words, " ")
    for (i = 1; i <= n; i++) {
        word = words[i]
        sub(/[.,;:]$/, "", word)
        if (length(word) >= 2 && substr(word, 1, 1) == "`" && substr(word, length(word)) == "`")
            word = substr(word, 2, length(word) - 2)
        while (substr(word, 1, 2) == "./")
            word = substr(word, 3)
        if (word != "")
            print word
    }
}
'

# The plan on stdin with the entry in $ENTRY added to "## Amendments", after
# the last line of the section that is not blank. Without the section, it
# first adds the heading, with one blank line before and after, above
# "## Implementation log", or at the end when there is no log.
AWK_AMEND='
BEGIN {
    entry = ENVIRON["ENTRY"]
}
{
    L[++n] = $0
    if ($0 ~ /^[ \t]*(```|~~~)/) {
        fence = !fence
        next
    }
    if (fence || $0 !~ /^## /)
        next
    if (in_amend && !amend_end)
        amend_end = n
    in_amend = 0
    if (!amend_at && $0 ~ /^## Amendments[ \t]*$/) {
        amend_at = n
        in_amend = 1
    } else if (!log_at && $0 ~ /^## Implementation log[ \t]*$/) {
        log_at = n
    }
}
END {
    if (amend_at) {
        if (!amend_end)
            amend_end = n + 1
        last = amend_at
        for (i = amend_at + 1; i < amend_end; i++)
            if (L[i] ~ /[^ \t]/)
                last = i
        for (i = 1; i <= last; i++)
            print L[i]
        if (last == amend_at)
            print ""
        print entry
        for (i = last + 1; i <= n; i++)
            print L[i]
    } else if (log_at) {
        for (i = 1; i < log_at; i++)
            print L[i]
        if (log_at == 1 || L[log_at - 1] ~ /[^ \t]/)
            print ""
        print "## Amendments"
        print ""
        print entry
        print ""
        for (i = log_at; i <= n; i++)
            print L[i]
    } else {
        for (i = 1; i <= n; i++)
            print L[i]
        if (n > 0 && L[n] ~ /[^ \t]/)
            print ""
        print "## Amendments"
        print ""
        print entry
    }
}
'

# git with no fsmonitor, no hooks and no replace refs: a repository setting
# must not run a program or swap an object under this tool.
run_git() {
    GIT_NO_REPLACE_OBJECTS=1 git -c core.fsmonitor=false -c core.hooksPath=/dev/null "$@"
}

# The snapshot command of the implementor: a throwaway index seeded from the
# real one, so neither the real index nor the stash list changes. Its git
# calls take the options of run_git as "$@"; the tree id is the same.
take_snapshot() {
    GIT_NO_REPLACE_OBJECTS=1 sh -c 'i="$(mktemp -u)"; cp "$(git "$@" rev-parse --git-path index)" "$i" 2>/dev/null; GIT_INDEX_FILE="$i" git "$@" add --all && GIT_INDEX_FILE="$i" git "$@" write-tree; s=$?; rm -f "$i"; exit $s' sh -c core.fsmonitor=false -c core.hooksPath=/dev/null
}

fail() {
    printf 'plan-tool.sh: %s\n' "$1" >&2
    exit 70
}

refuse() {
    printf 'plan-tool.sh: refused: %s\n' "$1" >&2
    exit 77
}

# 0 for a 40- or 64-hex object id.
is_object_id() {
    case $1 in
        '' | *[!0123456789abcdef]*) return 1 ;;
    esac
    [ ${#1} -eq 40 ] || [ ${#1} -eq 64 ]
}

check_step() {
    case $1 in
        '' | 0* | *[!0123456789]*) usage "not a step number: $1" ;;
    esac
}

# Print what the plan scanner prints for step $2 and field $3 of plan file
# $1; returns 3 when the plan has no such step. With $2 empty, the step list.
# With $4 set, a Verify prints the text after its label, not its spans.
plan_field() {
    LC_ALL=C awk -v N="$2" -v F="$3" -v RAW="${4:-}" "$AWK_SPANS$AWK_VERIFY" <"$1"
}

# Set steps_dir for $stem and refuse an unsafe work directory (exit 77). Run
# from the repository root.
check_work_dir() {
    steps_dir=.whygraph/plans/$stem/steps
    for part in .whygraph .whygraph/plans ".whygraph/plans/$stem" "$steps_dir"; do
        [ ! -h "$part" ] || refuse "$part is a symlink"
    done
    tracked=$(run_git ls-files -- ".whygraph/plans/$stem") || fail "git ls-files failed"
    [ -z "$tracked" ] || refuse "git tracks files under .whygraph/plans/$stem/"
}

# Refuse to write through $1 when it is a symlink or not a regular file.
check_leaf() {
    [ ! -h "$1" ] || refuse "$1 is a symlink"
    [ ! -e "$1" ] || [ -f "$1" ] || refuse "$1 is not a regular file"
}

# A whole-file write: start_write sets tmp to a new file beside $1, the
# caller fills it, and finish_write moves it over $1.
start_write() {
    check_leaf "$1"
    tmp=$(mktemp "${1%/*}/.plan-tool.XXXXXX") || fail "cannot write $1"
}

finish_write() {
    mv -f "$tmp" "$1" || {
        rm -f "$tmp"
        fail "cannot write $1"
    }
}

put_file() {
    start_write "$1"
    printf '%s\n' "$2" >"$tmp"
    finish_write "$1"
}

append_line() {
    check_leaf "$1"
    printf '%s\n' "$2" >>"$1" || fail "cannot append to $1"
}

# Copy blob $1 to the file $2 and check that the copy hashes back to $1, so
# an overwritten loose object is caught (exit 65). A replace ref for $1 is
# never followed, and it exits 65 too: something tried to swap the plan.
read_blob() {
    if ! run_git --no-replace-objects cat-file blob "$1" >"$2" 2>/dev/null ||
        [ "$(run_git hash-object --no-filters -- "$2")" != "$1" ] ||
        [ -n "$(run_git for-each-ref --format=x "refs/replace/$1")" ]; then
        printf 'plan-tool.sh: the saved plan was changed\n' >&2
        exit 65
    fi
}

# The probe test. Print "missing <word>" for each first word of a part of
# span $1 that is not a command, and "unplain <word>" for one that holds $, a
# backtick or a quote. A word with a slash that the Files text $2 names is a
# file a step creates, so it counts as one. With $3 set, a script that sh or
# bash reads is tested too, as a file: "missing <script>" when there is none
# and Files does not name it. After a cd, paths are not tested. A word
# reaches command -v as an argument, never as script text. Run from the
# repository root.
probe_misses() {
    printf '%s\n' "$1" | LC_ALL=C awk "$AWK_FIRST_WORDS" | {
        moved=
        while read -r kind word; do
            case $kind:$word in
                script:*[\$\`\'\"]*) continue ;;
                script:*)
                    [ -n "${3:-}" ] && [ -z "$moved" ] && [ ! -f "$word" ] || continue
                    [ -z "$(named_outside "$word" "$2")" ] || printf 'missing %s\n' "$word"
                    continue
                    ;;
                *:*[\$\`\'\"]*)
                    printf 'unplain %s\n' "$word"
                    continue
                    ;;
                *:*/*)
                    [ -z "$moved" ] && [ -n "$(named_outside "$word" "$2")" ] || continue
                    ;;
            esac
            sh -c 'command -v -- "$1"' probe "$word" </dev/null >/dev/null 2>&1 ||
                printf 'missing %s\n' "$word"
            [ "$word" != cd ] || moved=1
        done
    }
}

# Print path $1 unless the Files text $2 names it.
named_outside() {
    printf '%s\n' "$1" | FILES=$2 PREFIX= LC_ALL=C awk "$AWK_OUTSIDE"
}

# 0 when the first word of some part of span $1 fails the probe test: the
# test that end and baseline use for an exit of 126 or 127. $2 is the Files
# text.
cannot_run() {
    [ -n "$(probe_misses "$1" "$2")" ]
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

# Set budget from WHYGRAPH_VERIFY_TIMEOUT. A leading zero is refused, because
# $(( )) reads it as octal. Above 580 the budget is clamped, with a notice on
# stderr only, so the record does not change.
read_budget() {
    budget=${WHYGRAPH_VERIFY_TIMEOUT:-540}
    case $budget in
        0* | *[!0123456789]*) usage "WHYGRAPH_VERIFY_TIMEOUT is not a number of seconds: $budget" ;;
    esac
    if [ ${#budget} -gt 3 ] || [ "$budget" -gt 580 ]; then
        budget=580
        printf 'plan-tool.sh: WHYGRAPH_VERIFY_TIMEOUT clamped to 580 s\n' >&2
    fi
}

# The bounded job: it runs the commands of $1/spans in order, stops at the
# first failure, and appends to $1/ran and $1/output. stdin is closed so that
# a command cannot eat the list of spans.
BOUNDED_JOB='
code=0
while IFS= read -r span; do
    printf "command: %s\n" "$span" >>"$1/ran"
    sh -c "$span" >>"$1/output" 2>&1 </dev/null
    code=$?
    [ "$code" -eq 0 ] || break
done <"$1/spans"
exit "$code"
'

# True when set -m gives a background job its own process group. Without a
# terminal, dash accepts set -m and still keeps the job in the shell's group,
# so only the group id tells.
job_control_works() {
    (set -m) 2>/dev/null || return 1
    set -m
    sleep 5 >/dev/null 2>&1 &
    probe=$!
    probe_group=$(ps -o pgid= -p "$probe" 2>/dev/null | tr -d ' ')
    kill "$probe" 2>/dev/null
    wait "$probe"
    [ "$probe_group" = "$probe" ] && return 0
    set +m
    return 1
}

# Set kill_form: 1 setsid, 2 perl setpgrp, 3 set -m, 4 a walk of the job's
# children. The first that works wins; WHYGRAPH_KILL_FORM forces one (tests).
# Forms 1 to 3 give the job its own process group; form 4 misses a grandchild
# that left the job's tree.
choose_kill_form() {
    case ${WHYGRAPH_KILL_FORM:-} in
        '')
            if command -v setsid >/dev/null 2>&1; then
                kill_form=1
            elif command -v perl >/dev/null 2>&1; then
                kill_form=2
            elif job_control_works 2>/dev/null; then
                kill_form=3
            else
                kill_form=4
            fi
            ;;
        1) command -v setsid >/dev/null 2>&1 || usage "kill form 1 is not available" ;;
        2) command -v perl >/dev/null 2>&1 || usage "kill form 2 is not available" ;;
        3) job_control_works 2>/dev/null || usage "kill form 3 is not available" ;;
        4) ;;
        *) usage "not a kill form: $WHYGRAPH_KILL_FORM" ;;
    esac
    [ -z "${WHYGRAPH_KILL_FORM:-}" ] || kill_form=$WHYGRAPH_KILL_FORM
}

children_of() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -P "$1"
    else
        ps -A -o pid=,ppid= | LC_ALL=C awk -v p="$1" '$2 == p { print $1 }'
    fi
}

any_alive() {
    for pid in "$@"; do
        kill -0 "$pid" 2>/dev/null && return 0
    done
    return 1
}

# Kill the job $1 and everything it started: TERM, up to 5 s, then KILL.
kill_job() {
    if [ "$job_form" -ne 4 ]; then
        # Before setsid or setpgrp has run, the group does not exist yet.
        kill -TERM -"$1" 2>/dev/null || kill -TERM "$1" 2>/dev/null
        tries=0
        while [ "$tries" -lt 5 ] && kill -0 -"$1" 2>/dev/null; do
            sleep 1
            tries=$((tries + 1))
        done
        kill -KILL -"$1" 2>/dev/null
        return 0
    fi
    # Stop the tree first, so that no process can start a child the walk
    # misses. A stopped process keeps a TERM pending, so CONT comes before
    # the wait, not after the KILL.
    kill -STOP "$1" 2>/dev/null
    walked=" $1 "
    grew=1
    while [ "$grew" -eq 1 ]; do
        grew=0
        for pid in $walked; do
            for child in $(children_of "$pid"); do
                case $walked in
                    *" $child "*) ;;
                    *)
                        kill -STOP "$child" 2>/dev/null
                        walked="$walked$child "
                        grew=1
                        ;;
                esac
            done
        done
    done
    for pid in $walked; do kill -TERM "$pid" 2>/dev/null; done
    for pid in $walked; do kill -CONT "$pid" 2>/dev/null; done
    tries=0
    # shellcheck disable=SC2086  # one pid per word
    while [ "$tries" -lt 5 ] && any_alive $walked; do
        sleep 1
        tries=$((tries + 1))
    done
    for pid in $walked; do kill -KILL "$pid" 2>/dev/null; done
    return 0
}

# INT, TERM or HUP while a bounded job runs: kill the watchdog and the job,
# then exit 143. The kill runs in the background so this shell can reap the
# job, or its zombie would keep the wait for the full 5 s.
bounded_signal() {
    trap '' INT TERM HUP
    [ -z "$watchdog" ] || kill "$watchdog" 2>/dev/null
    if [ -n "$job" ]; then
        kill_job "$job" &
        killer=$!
        wait "$job"
        wait "$killer"
    fi
    [ -z "$watchdog" ] || wait "$watchdog"
    exit 143
}

# Run the commands of $1/spans as one job under the budget, with one
# watchdog. Fills $1/ran and $1/output and returns the job's exit code, or
# 124 when the watchdog killed it. read_budget and choose_kill_form run
# first, so their usage errors come before any command runs.
run_bounded() {
    : >"$1/ran"
    : >"$1/output"
    rm -f "$1/timed-out"
    job=
    watchdog=
    job_form=$kill_form
    # A subshell resets the caller's traps, so the trap is set here.
    trap bounded_signal INT TERM HUP
    # bash prints a notice when a background job dies of a signal, at its
    # next command; it must not reach the caller.
    {
        case $job_form in
            1) setsid sh -c "$BOUNDED_JOB" sh "$1" </dev/null >>"$1/output" 2>&1 & ;;
            2) perl -e 'setpgrp(0, 0); exec @ARGV or exit 127' sh -c "$BOUNDED_JOB" sh "$1" </dev/null >>"$1/output" 2>&1 & ;;
            *) sh -c "$BOUNDED_JOB" sh "$1" </dev/null >>"$1/output" 2>&1 & ;;
        esac
        job=$!
        if [ "$job_form" -eq 3 ]; then
            # Never kill a group that is not the job's own: after a failed
            # set -m it is the caller's.
            [ "$(ps -o pgid= -p "$job" 2>/dev/null | tr -d ' ')" = "$job" ] || job_form=4
        fi
        (
            sleeper=
            trap 'kill "$sleeper" 2>/dev/null; exit 0' TERM
            sleep "$budget" &
            sleeper=$!
            wait "$sleeper"
            trap '' TERM
            : >"$1/timed-out"
            kill_job "$job"
        ) </dev/null >/dev/null 2>&1 &
        watchdog=$!
        wait "$job"
        bounded=$?
        job=
        kill "$watchdog" 2>/dev/null
        wait "$watchdog"
        watchdog=
    } 2>/dev/null
    trap - INT TERM HUP
    [ "$kill_form" -ne 3 ] || set +m
    # An exit 0 that races the watchdog stays 0.
    if [ "$bounded" -ne 0 ] && [ -f "$1/timed-out" ]; then
        return 124
    fi
    return "$bounded"
}

# Run run_bounded on $work and set code. A shell runs no trap while it waits
# for a foreground subshell, so the job runs in a background one; run_bounded
# traps inside it. A subshell starts without job control, so form 3 turns
# it on again.
run_job() {
    (
        [ "$kill_form" -ne 3 ] || set -m
        run_bounded "$work"
    ) &
    sub=$!
    trap 'kill -TERM "$sub" 2>/dev/null; wait "$sub"; exit 143' INT TERM HUP
    wait "$sub" 2>/dev/null
    code=$?
    trap - INT TERM HUP
}

# The extra line of a record whose job the watchdog killed.
timed_out_line() {
    [ "$code" -ne 124 ] || [ ! -f "$work/timed-out" ] || printf 'timed out after %s s\n' "$budget"
}

cmd_fingerprint() {
    [ $# -eq 1 ] || usage
    # The paths are repo-relative. Outside a repo, hash-object still works.
    if root=$(run_git rev-parse --show-toplevel 2>/dev/null); then
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
            hash=$(run_git hash-object "$path" 2>/dev/null) || hash=missing
        fi
        printf '%s %s\n' "$hash" "$path"
        [ -n "$more" ] || break
    done | run_git hash-object --stdin
}

cmd_check() {
    [ $# -eq 1 ] || usage
    [ -f "$1" ] || usage "no such plan: $1"
    LC_ALL=C awk "$AWK_SPANS$AWK_CONSTRAINT$AWK_CHECK" <"$1"
}

# Take the end tree and write the record of the run in $work to $record.
write_record() {
    # "none" can never equal a 40-hex end tree, so the evaluator escalates.
    tree=$(take_snapshot) || tree=none
    start_write "$record"
    {
        cat "$work/ran"
        printf 'exit: %s\n' "$code"
        printf 'tree: %s\n' "$tree"
        timed_out_line
        printf '%s\n' '--- last 40 lines of output'
        tail -n 40 "$work/output"
    } >"$tmp"
    finish_write "$record"
}

cmd_verify() {
    [ $# -eq 2 ] || usage
    plan=$1
    step=$2
    check_step "$step"
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    plan_field "$plan" "$step" Verify >"$work/spans"
    [ $? -ne 3 ] || usage "no step $step in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    record=$steps_dir/$step.verify
    check_leaf "$record"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"

    if [ ! -s "$work/spans" ]; then
        put_file "$record" 'exit: not literal'
        printf 'verify: not literal\n'
        exit 0
    fi

    printf 'verify: literal\n'
    run_bounded "$work"
    code=$?
    write_record
    timed_out_line
    tail -n 40 "$work/output"
    exit "$code"
}

cmd_begin() {
    [ $# -eq 2 ] || usage
    plan=$1
    step=$2
    check_step "$step"
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    plan_field "$plan" "" "" | grep -qx "step $step" || usage "no step $step in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    attempts=$steps_dir/attempts
    start=$steps_dir/$step.t0
    check_leaf "$attempts"
    check_leaf "$start"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
    rm -f "$steps_dir/$step.done"

    # A stored start tree counts only for a step this tool began before.
    t0=
    if grep -q "^$step begin " "$attempts" 2>/dev/null; then
        t0=$(cat "$start" 2>/dev/null) && is_object_id "$t0" || t0=
    fi
    # A later step already ran: its trees no longer follow this one.
    if [ -e "$steps_dir/$((step + 1)).t0" ] || [ -h "$steps_dir/$((step + 1)).t0" ]; then
        t0=
        for later in "$steps_dir"/*.t0; do
            number=${later##*/}
            number=${number%.t0}
            case $number in
                '' | *[!0123456789]*) continue ;;
            esac
            [ "$number" -le "$step" ] || rm -f "$later"
        done
    fi
    if [ -z "$t0" ]; then
        t0=$(take_snapshot) && is_object_id "$t0" || fail "snapshot failed"
        put_file "$start" "$t0"
    fi
    append_line "$attempts" "$step begin $t0"
    printf '%s\n' "$t0"
}

cmd_end() {
    [ $# -eq 3 ] || [ $# -eq 4 ] || usage
    plan=$1
    step=$2
    t0=$3
    blob=${4:-}
    check_step "$step"
    is_object_id "$t0" || usage "not a tree id: $t0"
    [ $# -eq 3 ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    if [ -z "$blob" ]; then
        cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    fi
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    [ -z "$blob" ] || read_blob "$blob" "$work/plan"
    run_git cat-file -e "$t0^{tree}" 2>/dev/null || usage "no such tree: $t0"
    plan_field "$work/plan" "$step" Verify >"$work/spans"
    [ $? -ne 3 ] || usage "no step $step in $plan"
    plan_field "$work/plan" "$step" Files >"$work/files"
    check_work_dir
    record=$steps_dir/$step.verify
    end_tree=$steps_dir/$step.t1
    diff_file=$steps_dir/$step.diff
    for leaf in "$record" "$end_tree" "$diff_file"; do
        check_leaf "$leaf"
    done
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
    rm -f "$steps_dir/$step.done"

    if [ -s "$work/spans" ]; then
        run_job
        write_record
        shown=$code
    else
        put_file "$record" 'exit: not literal'
        code=0
        tree=none
        shown='not literal'
    fi
    # A record without a tree (not literal, or tree: none) gets end's own.
    if ! is_object_id "$tree"; then
        tree=$(take_snapshot) && is_object_id "$tree" || fail "snapshot failed"
    fi
    put_file "$end_tree" "$tree"
    start_write "$diff_file"
    run_git diff -U3 --no-color --no-ext-diff --no-textconv "$t0" "$tree" >"$tmp" || fail "git diff failed"
    finish_write "$diff_file"
    run_git -c core.quotePath=false diff --name-only --no-renames "$t0" "$tree" >"$work/changed" ||
        fail "git diff failed"

    printf 'end: %s %s exit %s\n' "$t0" "$tree" "$shown"
    FILES=$(cat "$work/files") PREFIX='outside Files: ' LC_ALL=C awk "$AWK_OUTSIDE" <"$work/changed"

    while IFS= read -r path; do
        case $path in
            .claude/skills/whygraph-plan/* | .cursor/skills/whygraph-plan/* | .github/whygraph/* | .codex/whygraph/*)
                printf 'plan-tool.sh: the step changed the plan tool: %s\n' "$path" >&2
                exit 65
                ;;
        esac
    done <"$work/changed"
    [ "$code" -ne 0 ] || exit 0
    [ "$code" -ne 124 ] || [ ! -f "$work/timed-out" ] || exit 124
    case $code in
        126 | 127)
            last=$(sed -n 's/^command: //p' "$work/ran" | tail -n 1)
            ! cannot_run "$last" "$(cat "$work/files")" || exit 4
            ;;
    esac
    exit 1
}

cmd_pass() {
    [ $# -eq 2 ] || usage
    plan=$1
    step=$2
    check_step "$step"
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    tree=$(cat "$steps_dir/$step.t1" 2>/dev/null) && is_object_id "$tree" ||
        usage "step $step has no end tree"
    put_file "$steps_dir/$step.done" "$tree"
}

# 0 when step $1 passed under an older version of the harness: no begin
# line, a "- [x]" log line, and a passing record whose tree equals <N>.t1.
logged_pass() {
    printf '%s\n' "$scan" | grep -qx "logged $1" || return 1
    ! grep -q "^$1 begin " "$steps_dir/attempts" 2>/dev/null || return 1
    tree=$(cat "$steps_dir/$1.t1" 2>/dev/null) && is_object_id "$tree" || return 1
    [ -f "$steps_dir/$1.verify" ] || return 1
    result=$(LC_ALL=C awk '
        /^--- / { exit }
        /^exit: / && !e { e = substr($0, 7) }
        /^tree: / && !t { t = substr($0, 7) }
        END { print e " " t }
    ' <"$steps_dir/$1.verify")
    [ "$result" = "0 $tree" ]
}

cmd_next() {
    [ $# -eq 1 ] || usage
    plan=$1
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    scan=$(plan_field "$plan" "" "")
    [ $? -ne 3 ] || usage "no steps in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    while read -r kind number; do
        [ "$kind" = step ] || continue
        [ -f "$steps_dir/$number.done" ] || logged_pass "$number" || {
            printf '%s\n' "$number"
            exit 0
        }
    done <<EOF
$scan
EOF
    printf 'done\n'
}

cmd_changed() {
    [ $# -eq 2 ] || usage
    is_object_id "$1" || usage "not a tree id: $1"
    is_object_id "$2" || usage "not a tree id: $2"
    run_git rev-parse --show-toplevel >/dev/null 2>&1 || usage "not in a git repository"
    for tree in "$1" "$2"; do
        run_git cat-file -e "$tree^{tree}" 2>/dev/null || usage "no such tree: $tree"
    done
    paths=$(run_git -c core.quotePath=false diff --name-only --no-renames "$1" "$2") ||
        fail "git diff failed"
    [ -n "$paths" ] || exit 0
    printf '%s\n' "$paths"
    exit 1
}

# The Files text of every step of plan file $1, joined by commas: a later
# Verify or a Check may run a file that an earlier step creates.
all_files() {
    plan_field "$1" "" "" | while read -r kind number; do
        [ "$kind" != step ] || plan_field "$1" "$number" Files
    done | LC_ALL=C tr '\n' ','
}

cmd_probe() {
    [ $# -eq 1 ] || usage
    plan=$1
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    plan_field "$work/plan" "" "" >"$work/scan"
    [ $? -ne 3 ] || usage "no steps in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir

    while read -r kind number; do
        case $kind in
            step) plan_field "$work/plan" "$number" Verify | sed "s/^/step $number: /" ;;
            item) plan_field "$work/plan" "$number" Check | sed "s/^/acceptance $number: /" ;;
        esac
    done <"$work/scan" >"$work/spans"
    files=$(all_files "$work/plan")

    # Every command first, so a person sees it before baseline runs it.
    sed 's/^/span: /' "$work/spans"
    : >"$work/misses"
    while IFS= read -r line; do
        where=${line%%: *}
        probe_misses "${line#*: }" "$files" scripts | while read -r kind word; do
            if [ "$kind" = unplain ]; then
                printf 'missing: %s (%s) (not a plain command)\n' "$word" "$where"
            else
                printf 'missing: %s (%s)\n' "$word" "$where"
            fi
        done >>"$work/misses"
    done <"$work/spans"
    cat "$work/misses"
    [ ! -s "$work/misses" ] || exit 4
}

# The last non-blank line of output file $1, tabs turned into spaces, cut at
# 200 bytes.
excerpt_of() {
    LC_ALL=C awk '/[^ \t]/ { last = $0 } END { gsub(/\t/, " ", last); print substr(last, 1, 200) }' <"$1"
}

# 0 when the Test first text $1 names a test: its first word, without
# quotes, backticks or a final stop and lowercased, is not "none". A
# missing field names none.
names_test_first() {
    word=$(printf '%s\n' "$1" | LC_ALL=C awk '{ print tolower($1); exit }')
    word=$(printf '%s' "$word" | LC_ALL=C tr -d '`"*')
    word=${word%[.,;:]}
    [ -n "$word" ] && [ "$word" != none ]
}

cmd_baseline() {
    [ $# -ge 2 ] && [ $# -le 4 ] || usage
    plan=$1
    step=$2
    shift 2
    first=
    blob=
    if [ $# -gt 0 ] && [ "$1" = first ]; then
        first=1
        shift
    fi
    if [ $# -gt 0 ]; then
        blob=$1
        shift
        is_object_id "$blob" || usage "not a blob id: $blob"
    fi
    [ $# -eq 0 ] || usage
    check_step "$step"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    if [ -z "$blob" ]; then
        cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    fi
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    [ -z "$blob" ] || read_blob "$blob" "$work/plan"
    plan_field "$work/plan" "$step" Verify >"$work/spans"
    [ $? -ne 3 ] || usage "no step $step in $plan"
    check_work_dir
    results=$steps_dir/baseline
    check_leaf "$results"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
    # first starts a pass: an empty file, written as a whole.
    if [ -n "$first" ]; then
        start_write "$results"
        finish_write "$results"
    fi

    # The key of the result: a changed Verify line gets a new one.
    hash=$(plan_field "$work/plan" "$step" Verify raw | run_git hash-object --stdin) ||
        fail "git hash-object failed"
    # A result of this pass for the same Verify line is copied, not run again.
    # A test first line ran nothing, so it is never copied.
    result=
    [ ! -f "$results" ] || result=$(LC_ALL=C awk -v key="$hash:" '
        $1 == "step" && $3 == key {
            r = substr($0, index($0, ": ") + 2)
            if (r != "test first") {
                print r
                exit
            }
        }
    ' <"$results")
    if names_test_first "$(plan_field "$work/plan" "$step" "Test first")"; then
        result='test first'
    elif [ ! -s "$work/spans" ]; then
        result='not literal'
    elif [ -z "$result" ]; then
        run_job
        last=$(sed -n 's/^command: //p' "$work/ran" | tail -n 1)
        if [ "$code" -eq 0 ]; then
            result=pass
            # A pass that ran no test says so.
            if LC_ALL=C awk "$AWK_NO_TEST" <"$work/output"; then
                result='pass (no test ran)'
            fi
        elif [ "$code" -eq 124 ] && [ -f "$work/timed-out" ]; then
            result="timeout $budget: $last"
        elif { [ "$code" -eq 126 ] || [ "$code" -eq 127 ]; } &&
            cannot_run "$last" "$(all_files "$work/plan")"; then
            result="cannot run $code: $last"
        else
            result="fail $code: $(excerpt_of "$work/output")"
        fi
    fi

    line="step $step $hash: $result"
    append_line "$results" "$line"
    printf '%s\n' "$line"
    case $result in
        'timeout '*) exit 3 ;;
        'cannot run '*) exit 4 ;;
    esac
    exit 0
}

cmd_accept() {
    [ $# -eq 2 ] || [ $# -eq 3 ] || usage
    plan=$1
    item=$2
    blob=${3:-}
    case $item in
        '' | 0* | *[!0123456789]*) usage "not an item number: $item" ;;
    esac
    [ $# -eq 2 ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    if [ -z "$blob" ]; then
        cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    fi
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    [ -z "$blob" ] || read_blob "$blob" "$work/plan"
    plan_field "$work/plan" "$item" Check >"$work/spans"
    [ $? -ne 3 ] || usage "no Acceptance item $item in $plan"
    check_work_dir
    record=$steps_dir/acceptance.$item
    check_leaf "$record"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"

    # A manual: or prose Check, or an item without one.
    if [ ! -s "$work/spans" ]; then
        put_file "$record" 'exit: not literal'
        printf 'accept: not literal\n'
        exit 0
    fi

    printf 'accept: literal\n'
    run_job
    write_record
    timed_out_line
    tail -n 40 "$work/output"
    [ "$code" -ne 0 ] || exit 0
    [ "$code" -ne 124 ] || [ ! -f "$work/timed-out" ] || exit 124
    exit 1
}

# The step's run counts. "<N> <kind>" lines since the last reset line, of any
# step, count; begin lines count for nothing.
cmd_attempt() {
    [ $# -eq 3 ] || [ $# -eq 4 ] || usage
    plan=$1
    step=$2
    kind=$3
    detail=${4:-}
    check_step "$step"
    case $kind in
        retry)
            [ $# -eq 4 ] || usage "retry takes a detail, such as sonnet:opus"
            # No character of a redirect or a quote reaches the command line.
            case $detail in
                '' | *[!abcdefghijklmnopqrstuvwxyz0123456789:-]*) usage "not a detail: $detail" ;;
            esac
            cap=1
            ;;
        findings)
            [ $# -eq 4 ] || usage "findings takes a cap of 0, 1 or 2"
            case $detail in
                0 | 1 | 2) cap=$detail ;;
                *) usage "not a findings cap: $detail" ;;
            esac
            detail=
            ;;
        constraint | reset)
            [ $# -eq 3 ] || usage "$kind takes no detail"
            cap=1
            ;;
        *) usage "not an attempt kind: $kind" ;;
    esac
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    plan_field "$plan" "" "" | grep -qx "step $step" || usage "no step $step in $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    attempts=$steps_dir/attempts
    check_leaf "$attempts"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"

    if [ "$kind" = reset ]; then
        append_line "$attempts" "$step reset"
        printf 'attempt: reset\n'
        exit 0
    fi
    used=0
    if [ -f "$attempts" ]; then
        used=$(LC_ALL=C awk -v n="$step" -v k="$kind" '
            $2 == "reset" { c = 0; next }
            $1 == n && $2 == k { c++ }
            END { print c + 0 }
        ' <"$attempts") || fail "cannot read $attempts"
    fi
    if [ "$used" -ge "$cap" ]; then
        printf 'attempt: step %s used its %s cap (%s)\n' "$step" "$kind" "$cap"
        exit 1
    fi
    append_line "$attempts" "$step $kind${detail:+ $detail}"
    printf 'attempt: %s %s of %s\n' "$kind" "$((used + 1))" "$cap"
}

# Set mine and all from the scanner output in file $1: step $step's lines and
# every line of "## Amendments".
count_amendments() {
    mine=$(grep -cx "amended $step" "$1")
    all=$(grep -c '^amended ' "$1")
}

cmd_amends() {
    [ $# -eq 2 ] || [ $# -eq 3 ] || usage
    plan=$1
    step=$2
    blob=${3:-}
    check_step "$step"
    [ $# -eq 2 ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    if [ -z "$blob" ]; then
        cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    fi
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    [ -z "$blob" ] || read_blob "$blob" "$work/plan"
    plan_field "$work/plan" "" "" >"$work/scan"
    grep -qx "step $step" "$work/scan" || usage "no step $step in $plan"
    count_amendments "$work/scan"
    printf 'amends: step %s has %s, the plan has %s\n' "$step" "$mine" "$all"
    [ "$mine" -eq 0 ] && [ "$all" -lt 3 ] || exit 1
}

# Refuse a symlinked plan $1 (exit 77).
check_plan_link() {
    [ ! -h "$1" ] || refuse "$1 is a symlink"
}

cmd_save() {
    [ $# -eq 1 ] || usage
    plan=$1
    check_plan_link "$plan"
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # One read: the blob is exactly the bytes this call saw.
    cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    blob=$(run_git hash-object -w --no-filters -- "$work/plan") && is_object_id "$blob" ||
        fail "git hash-object failed"
    printf '%s\n' "$blob"
}

# Move file $1 over the plan at the absolute path $plan, through a new file
# beside it that keeps the mode of the old plan.
replace_plan() {
    tmp=$(mktemp "${plan%/*}/.plan-tool.XXXXXX") || fail "cannot write $plan"
    [ ! -f "$plan" ] || cp -p "$plan" "$tmp" 2>/dev/null || :
    cat "$1" >"$tmp" || {
        rm -f "$tmp"
        fail "cannot write $plan"
    }
    mv -f "$tmp" "$plan" || {
        rm -f "$tmp"
        fail "cannot write $plan"
    }
}

# The plan's absolute path, from the caller's directory.
absolute_plan() {
    case $plan in
        /*) ;;
        *) plan=$PWD/$plan ;;
    esac
}

cmd_restore() {
    [ $# -eq 2 ] || usage
    plan=$1
    blob=$2
    is_object_id "$blob" || usage "not a blob id: $blob"
    check_plan_link "$plan"
    [ ! -e "$plan" ] || [ -f "$plan" ] || usage "not a plan file: $plan"
    case $plan in
        */*) [ -d "${plan%/*}" ] || usage "no such directory: ${plan%/*}" ;;
    esac
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    absolute_plan
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    read_blob "$blob" "$work/plan"
    check_plan_link "$plan"
    replace_plan "$work/plan"
}

cmd_log_check() {
    [ $# -eq 2 ] || usage
    plan=$1
    blob=$2
    is_object_id "$blob" || usage "not a blob id: $blob"
    check_plan_link "$plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # One read of the plan: the id printed is of exactly the checked bytes.
    present=1
    [ -f "$plan" ] && cat <"$plan" >"$work/new" 2>/dev/null || present=
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    read_blob "$blob" "$work/old"
    if [ -z "$present" ]; then
        printf 'log-check: the plan is missing\n'
        exit 1
    fi
    LC_ALL=C awk "$AWK_STRIP" <"$work/old" >"$work/old.kept"
    LC_ALL=C awk "$AWK_STRIP" <"$work/new" >"$work/new.kept"
    OLD=$work/old.kept LC_ALL=C awk "$AWK_SAME" <"$work/new.kept" || exit 1
    id=$(run_git hash-object -w --no-filters -- "$work/new") && is_object_id "$id" ||
        fail "git hash-object failed"
    printf '%s\n' "$id"
}

# One manifest line for file $2 under the label $1: its blob id and whether
# it can run, a link and its target, "other", or "none".
state_line() {
    if [ -h "$2" ]; then
        printf '%s link %s\n' "$1" "$(ls -l "$2" 2>/dev/null | sed 's/.* -> //')"
    elif [ -f "$2" ]; then
        id=$(run_git hash-object --no-filters -- "$2" 2>/dev/null) || id=unreadable
        mode=-
        [ ! -x "$2" ] || mode=x
        printf '%s %s %s\n' "$1" "$mode" "$id"
    elif [ -e "$2" ]; then
        printf '%s other\n' "$1"
    else
        printf '%s none\n' "$1"
    fi
}

state_manifest() {
    state_line config "$common/config"
    for hook in "$common"/hooks/*; do
        [ -e "$hook" ] || [ -h "$hook" ] || continue
        state_line "hook ${hook##*/}" "$hook"
    done
    state_line attempts "$steps_dir/attempts"
    state_line baseline "$steps_dir/baseline"
    for marker in "$steps_dir"/*.done; do
        [ -e "$marker" ] || [ -h "$marker" ] || continue
        state_line "done ${marker##*/}" "$marker"
    done
}

cmd_state() {
    [ $# -eq 1 ] || [ $# -eq 2 ] || usage
    plan=$1
    want=${2:-}
    [ $# -eq 1 ] || is_object_id "$want" || usage "not a state hash: $want"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    common=$(run_git rev-parse --git-common-dir) || fail "git rev-parse failed"
    now=$(state_manifest | run_git hash-object --stdin) && is_object_id "$now" ||
        fail "git hash-object failed"
    if [ -z "$want" ]; then
        printf '%s\n' "$now"
    elif [ "$now" = "$want" ]; then
        printf 'state: unchanged\n'
    else
        printf 'state: changed\n'
        exit 1
    fi
}

# Copied from guard.sh (has_shell_syntax), as text: the scripts stay apart.
# True when the text holds shell syntax. The first seven end or redirect a
# command, or expand to one. A single quote, a glob or a brace can put a
# word on the command line that a check of the literal text never saw.
has_shell_syntax() {
    case $1 in
        *[\;\&\|\<\>\$\`\'\*\?\[\{\}]*) return 0 ;;
    esac
    return 1
}

# True when command $1 is a git push. The pattern is guard.sh's: git, its
# own options and their arguments, then push.
names_git_push() {
    printf '%s\n' "$1" | LC_ALL=C tr '\t;&|' ' \n\n\n' | LC_ALL=C tr -s ' ' |
        sed 's/^[[:space:]]*//' | grep -Eq '^git( -[^ ]+( [^- ][^ ]*)?)* push( |$)'
}

refusal() {
    printf 'amend-check: %s\n' "$1" >>"$work/refusals"
}

# Set path to $1 without its leading ./ and return 0 when it is a concrete
# relative path: safe characters, no "..", no empty or "." part.
concrete_path() {
    path=$1
    while :; do
        case $path in
            ./*) path=${path#./} ;;
            *) break ;;
        esac
    done
    path_is_safe "$path" || return 1
    case /$path/ in
        // | *//* | */./*) return 1 ;;
    esac
    return 0
}

# 0 when the path $1 is in .whygraph/, .git/ or an agent directory.
reserved_path() {
    case $1 in
        .whygraph | .whygraph/* | .git | .git/* | .claude | .claude/* | .cursor | .cursor/* | .github | .github/* | .codex | .codex/*)
            return 0
            ;;
    esac
    return 1
}

# 0 when a part of the relative path $1 is a symlink.
has_symlink_part() {
    rest=$1
    part=
    while [ -n "$rest" ]; do
        case $rest in
            */*)
                part=$part${part:+/}${rest%%/*}
                rest=${rest#*/}
                ;;
            *)
                part=$part${part:+/}$rest
                rest=
                ;;
        esac
        [ ! -h "$part" ] || return 0
    done
    return 1
}

# 0 when some step's Files of the saved plan names $1 exactly.
in_files() {
    grep -qxF -- "$1" "$work/entries"
}

# 0 when the word $1 of a reviewed command is a repository path: not a flag,
# and holding a /, or holding a dot and naming a file or a Files entry. So a
# subcommand such as test or pytest never counts, even beside such a file.
old_is_path() {
    concrete_path "$1" || return 1
    case $path in
        */*) return 0 ;;
        *.*) [ -f "$path" ] || in_files "$path" ;;
        *) return 1 ;;
    esac
}

# 0 when $1 may be the new path of an amended command. Sets why otherwise.
new_path_ok() {
    concrete_path "$1" || {
        why="$1 is not a plain relative path"
        return 1
    }
    if reserved_path "$path"; then
        why="$path is under .whygraph/, .git/ or an agent directory"
    elif has_symlink_part "$path"; then
        why="$path has a symlink in it"
    elif [ ! -f "$path" ]; then
        why="$path is not a file"
    elif run_git check-ignore -q -- "$path" 2>/dev/null; [ $? -ne 1 ]; then
        why="$path is ignored by git"
    elif [ "$(GIT_LITERAL_PATHSPECS=1 run_git ls-files -- "$path" 2>/dev/null)" != "$path" ] &&
        ! in_files "$path"; then
        why="$path is not tracked and no step's Files names it"
    else
        return 0
    fi
    return 1
}

# 0 when the word $1 of a reviewed command may become $2. Sets why
# otherwise. A test id may change its file and its name; a path, its path.
swap_ok() {
    case $1 in
        *::*)
            name=${2#*::}
            case $name in
                '' | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-]*)
                    why="$2 is not a plain test id"
                    return 1
                    ;;
            esac
            old_is_path "${1%%::*}" || {
                why="$1 is not a test id"
                return 1
            }
            new_path_ok "${2%%::*}"
            ;;
        *)
            old_is_path "$1" || {
                why="$1 is not a path, so it cannot become $2"
                return 1
            }
            new_path_ok "$2"
            ;;
    esac
}

# The Verify rules of amend-check, for the new line of step $step.
amend_verify() {
    plan_field "$work/new" "$step" Verify >"$work/new.spans"
    case $new_value in
        manual:*)
            refusal "the new Verify is manual:"
            return
            ;;
    esac
    [ -s "$work/new.spans" ] || {
        refusal "the new Verify is not a literal command"
        return
    }
    while IFS= read -r span; do
        ! has_shell_syntax "$span" || refusal "shell syntax in the new Verify: $span"
        ! names_git_push "$span" || refusal "git push in the new Verify: $span"
    done <"$work/new.spans"
    [ ! -s "$work/refusals" ] || return

    # A literal Verify keeps its commands: the same number, at most one
    # re-pointed.
    plan_field "$work/old" "$step" Verify >"$work/old.spans"
    if [ -s "$work/old.spans" ]; then
        old_count=$(wc -l <"$work/old.spans")
        new_count=$(wc -l <"$work/new.spans")
        if [ "$old_count" -ne "$new_count" ]; then
            refusal "the new Verify has $((new_count)) commands, the reviewed one $((old_count))"
            return
        fi
        changed_spans=$(NEW="$work/new.spans" LC_ALL=C awk "$AWK_LINES" <"$work/old.spans" | grep -c '^line ')
        [ "$changed_spans" -le 1 ] || {
            refusal "the new Verify changes more than one command"
            return
        }
    fi

    # Every reviewed command: each step's Verify and each Acceptance Check.
    while read -r kind number; do
        case $kind in
            step) plan_field "$work/old" "$number" Verify ;;
            item) plan_field "$work/old" "$number" Check ;;
        esac
    done <"$work/scan" >"$work/reviewed"
    NEW_SPANS=$work/new.spans LC_ALL=C awk "$AWK_SWAPS" <"$work/reviewed" >"$work/swaps"
    index=0
    set -f
    while IFS= read -r span; do
        index=$((index + 1))
        matched=
        why=
        while read -r at pairs; do
            [ "$at" = "$index" ] || continue
            # shellcheck disable=SC2086  # one word per pair half
            set -- $pairs
            ok=1
            while [ $# -ge 2 ]; do
                swap_ok "$1" "$2" || {
                    ok=
                    break
                }
                shift 2
            done
            if [ -n "$ok" ]; then
                matched=1
                break
            fi
        done <"$work/swaps"
        [ -n "$matched" ] ||
            refusal "the new Verify runs a command no step or Check reviewed: $span${why:+ ($why)}"
    done <"$work/new.spans"
    set +f

    # A Test first step keeps running its test.
    test_first=$(plan_field "$work/old" "$step" "Test first")
    names_test_first "$test_first" || return
    printf '%s\n' "$test_first" | LC_ALL=C awk "$AWK_TOKENS" | grep '::' >"$work/test-ids"
    [ -s "$work/test-ids" ] || {
        refusal "step $step's Test first names no <file>::<name> test id"
        return
    }
    LC_ALL=C awk "$AWK_TOKENS" <"$work/new.spans" >"$work/new.words"
    while IFS= read -r id; do
        grep -qxF -- "$id" "$work/new.words" || refusal "the new Verify drops the Test first test $id"
    done <"$work/test-ids"
}

# The Files rules of amend-check, for the new line of step $step.
amend_files() {
    FILES=$old_value LIST=1 LC_ALL=C awk "$AWK_OUTSIDE" </dev/null >"$work/old.parts"
    FILES=$new_value LIST=1 LC_ALL=C awk "$AWK_OUTSIDE" </dev/null >"$work/new.parts"
    cut -f1 "$work/old.parts" >"$work/old.entries"
    cut -f1 "$work/new.parts" >"$work/new.entries"
    cut -f3 "$work/old.parts" >"$work/old.words"
    tab=$(printf '\t')
    while IFS=$tab read -r entry count words; do
        [ "$entry" = - ] || grep -qxF -- "$entry" "$work/new.entries" ||
            refusal "Files: $entry was removed"
    done <"$work/old.parts"

    # Words that name a path no step may touch, and the words of the Change.
    while read -r kind number; do
        [ "$kind" != step ] || plan_field "$work/old" "$number" Boundaries all
    done <"$work/scan" | LC_ALL=C awk "$AWK_TOKENS" >"$work/boundaries"
    plan_field "$work/old" "$step" Change all | LC_ALL=C awk "$AWK_TOKENS" >"$work/change"

    while IFS=$tab read -r entry count words; do
        ! grep -qxF -- "$words" "$work/old.words" || continue
        if [ "$count" -ne 1 ] || [ "$entry" = - ]; then
            refusal "Files: \"$words\" is not one path"
            continue
        fi
        ! grep -qxF -- "$entry" "$work/old.entries" || continue
        if ! concrete_path "$entry"; then
            refusal "Files: $entry is not a plain relative path"
            continue
        fi
        case $entry in
            */)
                refusal "Files: $entry is a directory"
                continue
                ;;
        esac
        if reserved_path "$path"; then
            refusal "Files: $path is under .whygraph/, .git/ or an agent directory"
        elif [ -d "$path" ]; then
            refusal "Files: $path is a directory"
        elif has_symlink_part "$path"; then
            refusal "Files: $path has a symlink in it"
        elif [ -e "$path" ] && [ ! -f "$path" ]; then
            refusal "Files: $path is not a regular file"
        elif ! grep -qxF -- "$path" "$work/change"; then
            refusal "Files: step $step's Change does not name $path"
        else
            while IFS= read -r word; do
                word=${word%/}
                case $path in
                    "$word" | "$word"/*)
                        refusal "Files: a step's Boundaries names $path"
                        break
                        ;;
                esac
            done <"$work/boundaries"
        fi
    done <"$work/new.parts"
}

cmd_amend_check() {
    [ $# -eq 3 ] || usage
    plan=$1
    blob=$2
    step=$3
    is_object_id "$blob" || usage "not a blob id: $blob"
    check_step "$step"
    check_plan_link "$plan"
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # One read of the plan; the awk programs get both copies as files.
    cat <"$plan" >"$work/new" || usage "cannot read $plan"
    absolute_plan
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    read_blob "$blob" "$work/old"
    plan_field "$work/old" "" "" >"$work/scan"
    grep -qx "step $step" "$work/scan" || usage "no step $step in the saved plan"
    all_files "$work/old" >"$work/files"
    FILES=$(cat "$work/files") LIST=1 LC_ALL=C awk "$AWK_OUTSIDE" </dev/null | cut -f1 >"$work/entries"
    : >"$work/refusals"

    count_amendments "$work/scan"
    [ "$mine" -eq 0 ] || refusal "step $step already has an amendment"
    [ "$all" -lt 3 ] || refusal "the plan already has 3 amendments"

    # Position: one line differs, and it is step N's Verify or Files line.
    NEW=$work/new LC_ALL=C awk "$AWK_LINES" <"$work/old" >"$work/diffs"
    [ "$(wc -l <"$work/old")" -eq "$(wc -l <"$work/new")" ] || printf 'count\n' >>"$work/diffs"
    field=
    verify_at=$(plan_field "$work/old" "$step" Verify line)
    files_at=$(plan_field "$work/old" "$step" Files line)
    if grep -qx count "$work/diffs"; then
        refusal "the amendment added or removed lines"
    elif [ ! -s "$work/diffs" ]; then
        refusal "the plan did not change"
    else
        changed=$(grep -c '^line ' "$work/diffs")
        while read -r what at; do
            if [ "$at" = "$verify_at" ]; then
                field=Verify
            elif [ "$at" = "$files_at" ]; then
                field=Files
            else
                refusal "line $at is not step $step's Verify or Files line: $(sed -n "${at}p" "$work/new")"
            fi
        done <"$work/diffs"
        [ "$changed" -eq 1 ] || {
            refusal "$changed lines changed; an amendment changes one"
            field=
        }
    fi
    if [ -n "$field" ]; then
        at=$(grep '^line ' "$work/diffs" | cut -d' ' -f2)
        old_value=$(plan_field "$work/old" "$step" "$field" raw)
        new_value=$(plan_field "$work/new" "$step" "$field" raw)
        old_line=$(sed -n "${at}p" "$work/old")
        new_line=$(sed -n "${at}p" "$work/new")
        if [ "$(plan_field "$work/new" "$step" "$field" line)" != "$at" ] ||
            [ "${old_line%"$old_value"}" != "${new_line%"$new_value"}" ]; then
            refusal "line $at is no longer step $step's $field line"
        else
            new_value=$(printf '%s\n' "$new_value" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
            old_value=$(printf '%s\n' "$old_value" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
            if [ "$new_value" = "$old_value" ]; then
                refusal "step $step's $field line changed only in blanks"
            elif [ "$field" = Verify ]; then
                amend_verify
            else
                amend_files
            fi
        fi
    fi
    if [ -s "$work/refusals" ]; then
        cat "$work/refusals"
        exit 1
    fi

    # The entry is built here from the two lines: no model text reaches it.
    stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    ENTRY="- Step $step: $field - $old_value -> $new_value ($stamp)" \
        LC_ALL=C awk "$AWK_AMEND" <"$work/new" >"$work/amended"
    id=$(run_git hash-object -w --no-filters -- "$work/amended") && is_object_id "$id" ||
        fail "git hash-object failed"
    check_plan_link "$plan"
    replace_plan "$work/amended"
    printf '%s\n' "$id"
}

# Search for each check of "## Constraint checks". The text and the glob are
# validated by AWK_CONSTRAINT and again here, and reach git grep as
# arguments after -e and --: no shell reads them.
cmd_constraints() {
    [ $# -ge 1 ] && [ $# -le 3 ] || usage
    plan=$1
    shift
    blob=
    step=
    if [ $# -gt 0 ] && is_object_id "$1"; then
        blob=$1
        shift
    fi
    if [ $# -gt 0 ]; then
        step=$1
        shift
        check_step "$step"
    fi
    [ $# -eq 0 ] || usage
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT

    # The plan path is relative to the caller, so read it before the cd.
    if [ -z "$blob" ]; then
        cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    fi
    cd "$root" || exit 1
    [ -z "$blob" ] || read_blob "$blob" "$work/plan"
    if [ -n "$step" ]; then
        plan_field "$work/plan" "" "" | grep -qx "step $step" || usage "no step $step in $plan"
    fi
    LC_ALL=C awk -v N="$step" "$AWK_CONSTRAINT$AWK_CONSTRAINTS" <"$work/plan" >"$work/checks" ||
        fail "cannot read the plan"
    if ! grep -qx section "$work/checks"; then
        printf 'constraints: the plan has no "## Constraint checks" section\n'
        exit 6
    fi
    LC_ALL=C awk -F '\t' '$1 == "bad" { printf "plan-tool.sh: line %s: %s\n", $2, $3 }' \
        <"$work/checks" >"$work/bad"
    if [ -s "$work/bad" ]; then
        cat "$work/bad" >&2
        exit 5
    fi
    exempt=" $(LC_ALL=C awk -F '\t' '$1 == "exempt" { printf "%s ", $2 }' <"$work/checks")"

    tab=$(printf '\t')
    held=0
    broken=0
    while IFS=$tab read -r kind id check_kind glob text; do
        [ "$kind" = check ] || continue
        case $exempt in
            *" $id "*) continue ;;
        esac
        # The second check: never trust the awk alone with a process call.
        case $glob in
            '' | /* | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/*-]*)
                printf 'plan-tool.sh: note #%s: %s\n' "$id" "Constraint check has a character outside the allowed set" >&2
                exit 5
                ;;
        esac
        case $text in
            *[\"\`\\]*)
                printf 'plan-tool.sh: note #%s: %s\n' "$id" "Constraint check has a character outside the allowed set" >&2
                exit 5
                ;;
        esac
        run_git -c core.quotePath=true grep -nIF --untracked --no-color -e "$text" -- ":(glob)$glob" \
            </dev/null >"$work/hits" 2>"$work/error"
        found=$?
        [ "$found" -le 1 ] || fail "git grep failed for note #$id: $(head -n 1 "$work/error")"
        if [ "$check_kind" = absent ] && [ "$found" -eq 0 ]; then
            where=$(LC_ALL=C awk '{ if (match($0, /:[0-9]+:/)) print substr($0, 1, RSTART + RLENGTH - 2); exit }' <"$work/hits")
            printf 'broken: note #%s %s\n' "$id" "$where"
            broken=$((broken + 1))
        elif [ "$check_kind" = present ] && [ "$found" -eq 1 ]; then
            printf 'broken: note #%s (no match)\n' "$id"
            broken=$((broken + 1))
        else
            held=$((held + 1))
        fi
    done <"$work/checks"
    printf 'constraints: %s hold, %s broken\n' "$held" "$broken"
    [ "$broken" -eq 0 ] || exit 1
}

cmd_brief() {
    [ $# -eq 1 ] || usage
    [ -f "$1" ] || usage "no such plan: $1"
    LC_ALL=C awk "$AWK_BRIEF" <"$1" || fail "cannot read $1"
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
    begin) cmd_begin "$@" ;;
    end) cmd_end "$@" ;;
    pass) cmd_pass "$@" ;;
    next) cmd_next "$@" ;;
    changed) cmd_changed "$@" ;;
    probe) cmd_probe "$@" ;;
    baseline) cmd_baseline "$@" ;;
    accept) cmd_accept "$@" ;;
    attempt) cmd_attempt "$@" ;;
    amends) cmd_amends "$@" ;;
    save) cmd_save "$@" ;;
    restore) cmd_restore "$@" ;;
    log-check) cmd_log_check "$@" ;;
    state) cmd_state "$@" ;;
    amend-check) cmd_amend_check "$@" ;;
    constraints) cmd_constraints "$@" ;;
    brief) cmd_brief "$@" ;;
    *) usage "unknown subcommand: $subcommand" ;;
esac
