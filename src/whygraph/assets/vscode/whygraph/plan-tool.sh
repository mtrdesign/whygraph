#!/bin/sh
# plan-tool.sh - the deterministic parts of the WhyGraph planning harness.
#
#   snapshot                    print the tree id of the working tree
#   stem <plan>                 print the plan's stem
#   fingerprint '<p1; p2>'      print the fingerprint of the listed files
#   check <plan>                lint a plan; 0 clean, 1 findings
#   verify <plan> <N>           run step N's literal Verify and record it
#   begin <plan> <N>            start step N; print its start tree
#   end <plan> <N> <t0> [<blob>] [--trust <hash>]
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
#   proof <plan>                print which steps proved their change
#   setup <plan> [<blob>]       run the "## Setup" commands once
#   format <plan> <t0> [<blob>] run the Format command on the files changed
#                               since tree <t0>
#   ready <plan> [<blob>]       say whether a failing baseline is a fault
#   dryrun <plan>               run every Verify and Acceptance item 1 at
#                               plan time; print the findings as a review
#   red <plan> <N> [again] [<blob>]
#                               run step N's Test first red, before the
#                               change; record it in steps/<N>.red
#   smoke <out path> <test file>
#                               run one existing browser test at plan time
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
# budget ran out. It exits 65 when the step changed a copy of this tool or
# of the capture script (the directory of either, in any of the four agent
# trees); 65 wins over the others. A <blob> that does not hash to its id exits 65.
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
# proof reads the plan and the records under steps/, runs no command and
# writes no file. It exits 0. Per step it reads the last baseline line and
# the exit of <N>.verify, and prints "proof: step <N> <class>". A Test first
# step takes its class from steps/<N>.red, after the guard. Each step's diff
# adds "tests:" pointer lines before the totals. It fails
# closed: only a check that went red before the change prints "proven". A
# step whose baseline failed because its Verify names a path or directory
# that an earlier step's Files name prints "carried". Two totals follow:
# the steps, and the Acceptance checks. baseline first empties the baseline
# file, so proof speaks for the last run only, and a step before
# --from-step prints "unknown". An "unproven" step is not a defect: a step
# that only moves code has no red test.
#
# setup runs the "## Setup" commands in order as one job under one budget,
# stops at the first that fails, and writes steps/setup. It exits 0 when
# all passed and no tracked or unignored file changed, 1 when a command
# failed, 3 at the deadline, 5 when the tree changed (the paths follow) and
# 6 when the plan has no "## Setup" section. ready reads steps/baseline and
# runs nothing: a resumed run prints "ready: resumed"; a "fail" whose
# Verify names a path, or a directory, of an earlier step's Files waits for
# that step; any other "fail" prints "not ready" and exits 1.
#
# A "- Format: `<command>`" bullet under "## Setup" is not a Setup command:
# setup never runs it, and the limit of 5 does not count it. check allows
# one. format runs it once, as setup runs its job, with each file that
# differs between tree <t0> and the working tree and still exists appended
# as a quoted argument. It leaves out the plan, .whygraph/, symlinks and
# each path that path_is_safe refuses, and runs nothing when no file is
# left. It prints "format: <k> files, exit <code>" and exits 0, 1 when the
# command failed, 3 at the deadline and 6 when the plan has no Format
# bullet. It writes no record.
#
# dryrun runs no agent, changes no tracked file and never writes
# steps/baseline: its record is steps/dryrun, and steps/setup when it runs
# Setup. One call ends within the Verify budget. A call with work left
# prints "dryrun: more, <k> of <n> commands done" and exits 2; the next call
# for the same plan goes on, and the sixth call always ends it. The finished
# call prints the findings in the plan reviewer's reply form, with a "- ran:"
# line, and exits 0. A changed plan or a finished record starts again.
#
# red runs the Verify of a Test first step once, before the change, after it
# copies the test file to testfirst/<N>/. It exits 0 for "red", "red
# (unnamed)", "red (did not run)", a recorded red and "not run: the code
# changed before the red run"; 1 when the test passed; 4 when the Verify
# cannot run; 5 when the test file is unsafe, missing or lacks the test, or
# (with again) the code changed; 6 without a Test first or a literal Verify;
# 124 at the deadline. It never writes <N>.verify. verify and end compare the
# test's block with that copy (the guard): an edit prints "verify: test first
# edited" first, and verify runs nothing and exits 5; end exits 5, and 65
# still wins. The block is found by indent, so a test that closes on a
# shallower line than it opens gets a shorter block: the guard can miss an
# edit there, and never reports a false one.
#
# A red Browser test line makes red run the capture script's --e2e mode on
# that file instead of the Verify; its record has "kind: browser" and the
# guard compares the whole file. smoke runs one existing browser test once
# at plan time, writes steps/smoke and prints "smoke: pass", "smoke: fail:
# <last line>", "smoke: timeout" or "smoke: not available: <reason>".
# begin also prints "trust: <hash>" and writes steps/<N>.trust: the hash of
# the plan tool's directory, the capture script, the UI profile and saved
# login, and the red records and snapshots of every earlier step. end with
# --trust <hash> exits 65 when that hash changed, and names the paths when
# steps/<N>.trust still hashes to the passed value.
#
# Every bounded job gets npm_config_yes=false and loses WHYGRAPH_UI_USER and
# WHYGRAPH_UI_PASSWORD, whatever WHYGRAPH_KEEP_SECRETS says: only the
# capture script reads the login. Its process group is killed after it
# exits. A setup or dryrun job also loses *_API_KEY,
# AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, CLAUDE_CODE_OAUTH_TOKEN and
# WHYGRAPH_*, unless the caller set WHYGRAPH_KEEP_SECRETS=1.
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
       plan-tool.sh end <plan> <N> <t0> [<blob>] [--trust <hash>]
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
       plan-tool.sh proof <plan>
       plan-tool.sh setup <plan> [<blob>]
       plan-tool.sh format <plan> <t0> [<blob>]
       plan-tool.sh ready <plan> [<blob>]
       plan-tool.sh dryrun <plan>
       plan-tool.sh red <plan> <N> [again] [<blob>]
       plan-tool.sh smoke <out path> <test file>
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
# ("amended ?" for any other "- " line there). A Verify or Check whose first
# span starts with "manual:" prints no span: it is not literal. With N empty
# and F "Setup", it prints the command of each "## Setup" bullet, and
# returns 3 when the plan has no such section. A "- Format:" bullet is not a
# Setup command: F "Format" prints its command, and returns 3 without one.
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
    in_setup = ($0 ~ /^## Setup[ \t]*$/)
    if (F == "Setup" && in_setup)
        found = 1
    next
}
# F "Setup" prints the one span of each bullet under "## Setup", except
# the Format bullet. F "Format" prints the span of the Format bullet.
F == "Setup" || F == "Format" {
    if (in_setup && $0 ~ /^[ \t]*- / && spans_of($0) == 1 && SPAN[1] ~ /[^ \t]/) {
        if ($0 !~ /^[ \t]*- Format:/) {
            if (F == "Setup")
                print SPAN[1]
        } else if (F == "Format") {
            print SPAN[1]
            found = 1
        }
    }
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
        if (RAW == "raw") {
            print substr($0, at + 10)
            exit
        }
        count = literal_spans(substr($0, at + 10))
        # A span that starts with "manual:" is never a command.
        if (count > 0 && index(SPAN[1], "manual:") == 1)
            count = 0
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
        if (count > 0 && index(SPAN[1], "manual:") == 1)
            count = 0
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
    KNOWN_FIELD["Browser test"] = 1
    n = split("haiku,sonnet,opus,fable", words, ",")
    for (i = 1; i <= n; i++)
        MODEL[words[i]] = 1
    PHRASE_COUNT = split(" and then ; and also ;, then ", PHRASE, ";")
    QUOTED_FIELD["Constraints to preserve"] = 1
    QUOTED_FIELD["Risks"] = 1
    QUOTED_FIELD["Changes on purpose"] = 1
    JOURNEY_LABEL_COUNT = split("Actor,Entry,Hops,Outcome,Leaves early,Waits", JOURNEY_LABEL, ",")
    for (i = 1; i <= JOURNEY_LABEL_COUNT; i++)
        KNOWN_JOURNEY_LABEL[JOURNEY_LABEL[i]] = 1
    LEAVES_COUNT = split("Refresh,Back,Cancel,Direct link", LEAVES, ",")
    # The characters of has_shell_syntax.
    SHELL_CHARS = ";&|<>$`\047*?[{}"

    section = ""
    field = ""
}

function finding(at, message) {
    printf "line %d: %s\n", at, message
    any_finding = 1
}

# An HTML comment, a zero-width character or a bidi control: the person reads
# the plan rendered, an agent reads it raw.
function hidden_text(s,    i) {
    if (!ZERO_WIDTHS) {
        ZERO_WIDTHS = split("\342\200\213 \342\200\214 \342\200\215 \342\201\240 \357\273\277", ZERO_WIDTH, " ")
        constraint_of("")
    }
    if (index(s, "<!--") > 0)
        finding(NR, "plan has hidden text: HTML comment")
    for (i = 1; i <= ZERO_WIDTHS; i++)
        if (index(s, ZERO_WIDTH[i]) > 0) {
            finding(NR, "plan has hidden text: zero-width character")
            break
        }
    for (i = 1; i <= SEPARATORS; i++)
        if (index(s, SEPARATOR[i]) > 0) {
            finding(NR, "plan has hidden text: bidi control")
            break
        }
}

# A bullet under "## Setup": one literal command, no shell syntax, no git push.
# The one Format bullet does not count toward the limit of 5.
function on_setup_line(s,    span, i, words, n, part) {
    if (s !~ /^[ \t]*- /)
        return
    if (s ~ /^[ \t]*- Format:/) {
        if (++format_count == 2)
            finding(NR, "Setup has more than one Format bullet")
    } else
        setup_count++
    if (spans_of(s) != 1 || SPAN[1] !~ /[^ \t]/) {
        finding(NR, "Setup line has no literal command")
        return
    }
    span = SPAN[1]
    for (i = 1; i <= length(SHELL_CHARS); i++)
        if (index(span, substr(SHELL_CHARS, i, 1)) > 0) {
            finding(NR, "Setup command has shell syntax: " span)
            break
        }
    gsub(/\t/, " ", span)
    gsub(/[;&|]/, "\n", span)
    n = split(span, words, "\n")
    for (i = 1; i <= n; i++) {
        part = words[i]
        gsub(/  +/, " ", part)
        sub(/^ +/, "", part)
        if (part ~ /^git( -[^ ]+( [^- ][^ ]*)?)* push( |$)/) {
            finding(NR, "Setup command names git push")
            break
        }
    }
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
        if (step_browser_red && step_test_first)
            finding(step_at, "Browser test red and Test first on one step")
    }
    step_browser_red = 0
    step_test_first = 0
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

# The Browser test line: "<path> - red" or "<path> - green" (plan 56 5.11).
function check_browser_test(value, at,    path, kind) {
    sub(/^[ \t]+/, "", value)
    sub(/[ \t]+$/, "", value)
    gsub(/`/, "", value)
    if (value !~ / - (red|green)$/) {
        finding(at, "Browser test is not \"<path> - red\" or \"<path> - green\"")
        return
    }
    kind = substr(value, length(value) - 2) == "red" ? "red" : "green"
    path = substr(value, 1, length(value) - length(kind) - 3)
    if (path == "" || path ~ /^[\/-]/ || path ~ /[^A-Za-z0-9._\/-]/ || ("/" path "/") ~ /\/\.\.\//) {
        finding(at, "Browser test path is not a plain relative path")
        return
    }
    BROWSER_PATH[path] = 1
    if (kind == "red") {
        BROWSER_RED[path] = 1
        step_browser_red = 1
    } else if (!(path in BROWSER_RED)) {
        finding(at, "Browser test green has no red step before it")
    }
}

function check_field(name, value, at,    n, words, word, i) {
    if (name == "Browser test") {
        check_browser_test(value, at)
    } else if (name == "Test first") {
        word = value
        gsub(/[`"*]/, "", word)
        sub(/^[ \t]+/, "", word)
        step_test_first = (tolower(word) !~ /^none([.,;:]|[ \t]|$)/ && word != "")
    } else if (name == "Model") {
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
        for (i = 1; i <= n; i++) {
            STEP_ROUTE[step_no, SPAN[i]] = 1
            if (SPAN[i] !~ ROUTE_RE)
                finding(at, "Routes path fails the route pattern: " SPAN[i])
        }
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
        STEP_SEEN[step_no] = 1
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

# The "## User journey" checks. The section comes before "## Steps" and
# "## Open questions", so the scan keeps each step citation, hop and (Q<n>)
# with its line number, and journey_end compares them after the last step.
# A hop line reads "- H<n>: `<from>` - <action> - `<to>` [(<state>)] - <step
# N | exists path:LN | owned by tN>": its first two backticked spans are its
# routes, and its step is the last "step N" after them. The Entry route is the
# first span of its line, and the Outcome route the last span of its line.
# A bare unknown is the lowercase word unknown on a line of the section,
# outside backticked spans and double-quoted text. An assumed part reads
# "assumed <default> (Q<n>)", which never holds that word.

# The text after the n-th backtick of s, or "" when s has fewer.
function after_ticks(s, n,    at) {
    for (; n > 0; n--) {
        at = index(s, "`")
        if (at == 0)
            return ""
        s = substr(s, at + 1)
    }
    return s
}

# The number of the last "step <N>" in s, or "".
function last_step(s,    number) {
    number = ""
    s = " " s
    while (match(s, /[^A-Za-z][Ss]tep [0-9]+/)) {
        number = substr(s, RSTART + 6, RLENGTH - 6)
        s = substr(s, RSTART + RLENGTH)
    }
    return number
}

function journey_route(span, at) {
    if (span !~ ROUTE_RE)
        finding(at, "User journey route fails the route pattern: " span)
}

# Whether token is a relative path:line with no ".." part that does not
# start with "-".
function safe_path_line(token,    path, number, n, parts, i) {
    path = token
    sub(/:[^:]*$/, "", path)
    number = token
    sub(/^.*:/, "", number)
    if (path == "" || number !~ /^[0-9]+$/)
        return 0
    if (substr(path, 1, 1) == "/" || substr(path, 1, 1) == "-")
        return 0
    n = split(path, parts, "/")
    for (i = 1; i <= n; i++)
        if (parts[i] == "..")
            return 0
    return 1
}

function on_journey_line(s,    label, value, count, name, text, at, token) {
    if (s ~ /^[ \t]*-[ \t]+\*\*[A-Z][A-Za-z ]*:\*\*/) {
        label = s
        sub(/^[ \t]*-[ \t]+\*\*/, "", label)
        sub(/:\*\*.*$/, "", label)
        value = s
        sub(/^[ \t]*-[ \t]+\*\*[^:]*:\*\*/, "", value)
        journey_label = label
        if (label in JOURNEY_LABEL_AT) {
            if (label in KNOWN_JOURNEY_LABEL)
                finding(NR, "User journey has more than one " label)
        } else {
            JOURNEY_LABEL_AT[label] = NR
            if (label == "Leaves early") {
                journey_leaves = value
            } else if (label == "Entry" || label == "Outcome") {
                count = spans_of(value)
                if (count <= 0) {
                    finding(NR, "User journey " label " has no route")
                } else if (label == "Entry") {
                    journey_entry = SPAN[1]
                    journey_route(journey_entry, NR)
                } else {
                    journey_outcome = SPAN[count]
                    journey_route(journey_outcome, NR)
                }
            }
        }
    } else if (s ~ /^[ \t]*-[ \t]+H[0-9]+:/) {
        journey_label = "H"
        name = s
        sub(/^[ \t]*-[ \t]+/, "", name)
        name = substr(name, 1, index(name, ":") - 1)
        HOP_AT[++hops] = NR
        HOP_NAME[hops] = name
        if (spans_of(s) < 2) {
            finding(NR, "User journey hop " name " needs a from-route and a to-route")
        } else {
            HOP_OK[hops] = 1
            HOP_FROM[hops] = SPAN[1]
            HOP_TO[hops] = SPAN[2]
            journey_route(SPAN[1], NR)
            journey_route(SPAN[2], NR)
            HOP_STEP[hops] = last_step(after_ticks(s, 4))
        }
    } else if (journey_label == "Leaves early") {
        journey_leaves = journey_leaves " " s
    }

    text = " " s
    while (match(text, /[^A-Za-z][Ss]tep [0-9]+/)) {
        CITE_AT[++cites] = NR
        CITE_STEP[cites] = substr(text, RSTART + 6, RLENGTH - 6)
        text = substr(text, RSTART + RLENGTH)
    }
    text = s
    while (match(text, /\(Q[0-9]+\)/)) {
        QREF_AT[++qrefs] = NR
        QREF_NO[qrefs] = substr(text, RSTART + 2, RLENGTH - 3)
        text = substr(text, RSTART + RLENGTH)
    }
    text = s
    while ((at = index(text, "exists ")) > 0) {
        text = substr(text, at + 7)
        token = text
        sub(/^[ \t]+/, "", token)
        sub(/[ \t].*$/, "", token)
        gsub(/`/, "", token)
        sub(/[.,;)]+$/, "", token)
        if (index(token, ":") > 0 && !safe_path_line(token))
            finding(NR, "User journey exists span is not a safe path:line: " token)
    }
    text = s
    gsub(/`[^`]*`/, " ", text)
    gsub(/"[^"]*"/, " ", text)
    if ((" " text " ") ~ /[^A-Za-z0-9_]unknown[^A-Za-z0-9_]/)
        finding(NR, "User journey has a bare unknown")
}

# The checks that need the whole plan, run in END after close_step.
function journey_end(    i, k, from, number, exempt) {
    if (!journey_at)
        return
    for (i = 1; i <= JOURNEY_LABEL_COUNT; i++)
        if (!(JOURNEY_LABEL[i] in JOURNEY_LABEL_AT))
            finding(journey_at, "User journey misses " JOURNEY_LABEL[i])
    if (!hops)
        finding(journey_at, "User journey has no hop")
    if ("Leaves early" in JOURNEY_LABEL_AT)
        for (i = 1; i <= LEAVES_COUNT; i++)
            if (index(journey_leaves, LEAVES[i] ":") == 0)
                finding(JOURNEY_LABEL_AT["Leaves early"], "User journey Leaves early misses " LEAVES[i])

    # The chain. A hop without both routes breaks it, so the next is not compared.
    from = journey_entry
    for (k = 1; k <= hops; k++) {
        if (!HOP_OK[k]) {
            from = ""
            continue
        }
        if (from != "" && HOP_FROM[k] != from)
            finding(HOP_AT[k], "User journey hop " HOP_NAME[k] " starts at " HOP_FROM[k] ", not at " from)
        from = HOP_TO[k]
    }
    if (hops && from != "" && journey_outcome != "" && journey_outcome != from)
        finding(JOURNEY_LABEL_AT["Outcome"], "User journey Outcome is on " journey_outcome ", not on the last hop target " from)

    for (k = 1; k <= cites; k++)
        if (!(CITE_STEP[k] in STEP_SEEN))
            finding(CITE_AT[k], "User journey names step " CITE_STEP[k] ", which is not among the step numbers")

    # A hop target needs the Routes of its step, unless the user was there
    # before: the Entry route or the from-route of an earlier hop.
    for (k = 1; k <= hops; k++) {
        number = HOP_STEP[k]
        if (!HOP_OK[k] || number == "" || !(number in STEP_SEEN) || HOP_TO[k] !~ ROUTE_RE)
            continue
        exempt = (HOP_TO[k] == journey_entry)
        for (i = 1; i < k; i++)
            if (HOP_OK[i] && HOP_FROM[i] == HOP_TO[k])
                exempt = 1
        if (!exempt && !((number, HOP_TO[k]) in STEP_ROUTE))
            finding(HOP_AT[k], "User journey hop " HOP_NAME[k] ": step " number " Routes lack " HOP_TO[k])
    }

    for (k = 1; k <= qrefs; k++)
        if (!(QREF_NO[k] in QUESTION_SEEN))
            finding(QREF_AT[k], "User journey names Q" QREF_NO[k] ", which no question section holds")

    if (!journey_walk)
        finding(acceptance_at ? acceptance_at : journey_at, "Acceptance has no walk of ## User journey")
}

{
    line = $0
    hidden_text(line)
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
            if (!acceptance_at)
                acceptance_at = NR
        } else if (line ~ /^## Open questions/) {
            section = "questions"
        } else if (line ~ /^## Risks called out/) {
            section = "risks"
        } else if (line ~ /^## Confidence:/) {
            have["## Confidence:"] = 1
        } else if (line ~ /^## Constraint checks[ \t]*$/) {
            section = "constraints"
        } else if (line ~ /^## User journey[ \t]*$/) {
            section = "journey"
            if (!journey_at)
                journey_at = NR
        } else if (line ~ /^## Setup[ \t]*$/) {
            section = "setup"
            if (!setup_at)
                setup_at = NR
        } else if (line ~ /^## Resolved decisions[ \t]*$/) {
            section = "resolved"
        }
    } else if (section == "journey") {
        on_journey_line(line)
    } else if (section == "setup") {
        on_setup_line(line)
    } else if (section == "resolved") {
        if (line ~ /^\|[ \t]*Q[0-9]+[ \t]*\|/) {
            number = line
            sub(/^\|[ \t]*Q/, "", number)
            sub(/[^0-9].*$/, "", number)
            QUESTION_SEEN[number] = 1
        }
    } else if (section == "constraints") {
        if (line ~ /[^ \t]/ && constraint_finding(constraint_of(line)) != "")
            finding(NR, constraint_finding(constraint_of(line)))
    } else if (section == "steps") {
        on_step_line(line)
    } else if (section == "acceptance") {
        on_acceptance_line(line)
        if (in_item && index(line, "walk the journey of ## User journey") > 0)
            journey_walk = 1
        # An e2e: Check stands in for the walk (plan 56 W1).
        if (in_item && line ~ /\*\*Check:\*\*[ \t]*e2e:/) {
            journey_walk = 1
            e2e = line
            sub(/^.*\*\*Check:\*\*[ \t]*e2e:[ \t]*/, "", e2e)
            sub(/[ \t].*$/, "", e2e)
            E2E_CHECK[e2e] = NR
        }
    } else if (section == "questions") {
        if (line ~ /^- \*\*Q[0-9]+\.\*\*/) {
            number = substr(line, 6)
            sub(/[^0-9].*$/, "", number)
            QUESTION_SEEN[number] = 1
        }
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
    journey_end()
    if (setup_count > 5)
        finding(setup_at, "Setup has more than 5 commands")
    for (e2e in E2E_CHECK)
        if (!(e2e in BROWSER_PATH))
            finding(E2E_CHECK[e2e], "Check e2e: names a path no step\047s Browser test line holds")
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

# The collection-error phrases that only red reads: a test that could not be
# collected, built or imported did not run. Matched as AWK_NO_TEST matches.
AWK_NOT_COLLECTED='
BEGIN {
    count = split("found no collectors|error collecting|error: not found|[build failed]|no test files|err_module_not_found|cannot find module|no test suite found|1..0", PHRASE, "|")
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

# 0 when a line of stdin holds a failure word: "fail", "error", "not ok", or
# a mark a runner prints (U+2715 to U+2718, U+00D7, U+25CF, as bytes). With
# NAME set, only a line that holds the test name counts, and the name is
# removed first, so test_error_message does not match itself. A name needs
# no letter, digit or _ right before or after it.
AWK_FAILURE='
BEGIN {
    name = ENVIRON["NAME"]
    marks = split("\342\234\225 \342\234\226 \342\234\227 \342\234\230 \303\227 \342\227\217", MARK, " ")
}
function strip_name(s,    out, at, before, after) {
    out = ""
    held = 0
    while ((at = index(s, name)) > 0) {
        before = (at > 1) ? substr(s, at - 1, 1) : ""
        after = substr(s, at + length(name), 1)
        if (before !~ /[A-Za-z0-9_]/ && after !~ /[A-Za-z0-9_]/) {
            held = 1
            out = out substr(s, 1, at - 1) " "
        } else {
            out = out substr(s, 1, at + length(name) - 1)
        }
        s = substr(s, at + length(name))
    }
    return out s
}
{
    s = $0
    if (name != "") {
        s = strip_name(s)
        if (!held)
            next
    }
    low = tolower(s)
    if (index(low, "fail") || index(low, "error") || index(low, "not ok")) {
        found = 1
        exit
    }
    for (i = 1; i <= marks; i++)
        if (index(s, MARK[i])) {
            found = 1
            exit
        }
}
END {
    exit found ? 0 : 1
}
'

# The block of the test named NAME in the file on stdin, printed line by
# line, or nothing when the file has no such line. The name line is the first
# definition: a quote right before the name, or def, fn, func or void. Else
# the first line that holds the name. No letter, digit or _ may follow the
# name. Decorator lines above it (@ or #[, also a multi-line one that closes
# before the name line) belong to it, and the lines below it up to the next
# non-blank line whose indent is not deeper, that line too when it starts
# with }, ) or ].
AWK_BLOCK='
function holds(s,    at, after, rest) {
    rest = s
    while ((at = index(rest, NAME)) > 0) {
        after = substr(rest, at + length(NAME), 1)
        if (after !~ /[A-Za-z0-9_]/) {
            HELD_AT = length(s) - length(rest) + at
            return 1
        }
        rest = substr(rest, at + length(NAME))
    }
    return 0
}
function definition(s,    before) {
    before = substr(s, 1, HELD_AT - 1)
    return before ~ /["\047`]$/ || before ~ /(^|[^A-Za-z0-9_])(def|fn|func|void)[ \t]+$/
}
function indent(s) {
    match(s, /^[ \t]*/)
    return RLENGTH
}
function decorator(s) {
    sub(/^[ \t]+/, "", s)
    return substr(s, 1, 1) == "@" || substr(s, 1, 2) == "#["
}
function balance(from, to,    i, s, n) {
    n = 0
    for (i = from; i <= to; i++) {
        s = L[i]
        n += gsub(/[(\[]/, "", s)
        s = L[i]
        n -= gsub(/[)\]]/, "", s)
    }
    return n
}
BEGIN { NAME = ENVIRON["NAME"] }
{ L[NR] = $0 }
END {
    first = 0
    for (i = 1; i <= NR; i++)
        if (holds(L[i])) {
            if (!first)
                first = i
            if (definition(L[i])) {
                at = i
                break
            }
        }
    if (!at)
        at = first
    if (!at)
        exit
    start = at
    i = at - 1
    while (i >= 1 && L[i] ~ /[^ \t]/) {
        if (decorator(L[i])) {
            start = i
            i--
            continue
        }
        found = 0
        for (j = i - 1; j >= 1 && j >= i - 20 && L[j] ~ /[^ \t]/; j--)
            if (decorator(L[j]) && balance(j, i) == 0) {
                found = j
                break
            }
        if (!found)
            break
        start = found
        i = found - 1
    }
    deep = indent(L[at])
    stop = at
    for (i = at + 1; i <= NR; i++) {
        if (L[i] !~ /[^ \t]/) {
            continue
        }
        if (indent(L[i]) <= deep) {
            t = L[i]
            sub(/^[ \t]+/, "", t)
            if (t ~ /^[})\]]/)
                stop = i
            break
        }
        stop = i
    }
    for (i = start; i <= stop; i++)
        print L[i]
}
'

# The skip markers of section 5.36, as fixed text with no letter or digit
# right before them. Prints each line of stdin that holds one.
AWK_MARKERS='
BEGIN {
    count = split(".skip(|.only(|.todo(|xit(|xdescribe(|xtest(|fit(|fdescribe(|pytest.mark.skip|pytest.mark.xfail|pytest.skip(|unittest.skip|@Disabled|@Ignore|t.Skip(|#[ignore]", MARKER, "|")
}
{
    for (i = 1; i <= count; i++) {
        rest = $0
        while ((at = index(rest, MARKER[i])) > 0) {
            before = (at > 1) ? substr(rest, at - 1, 1) : ""
            # A marker that starts with "." follows a name, as in it.skip(.
            if (substr(MARKER[i], 1, 1) == "." || before !~ /[A-Za-z0-9]/) {
                print $0
                next
            }
            rest = substr(rest, at + length(MARKER[i]))
        }
    }
}
'

# The test-change pointers of proof, from the diff of step STEP on stdin
# (section 5.36). A test file has a part tests, test, __tests__, e2e or
# cypress, or a name that starts with test_ or holds _test., .test. or .spec.
# Each line is a pointer for a person, never a verdict.
AWK_TEST_CHANGES='
BEGIN {
    STEP = ENVIRON["STEP"]
    count = split(".skip(|.only(|.todo(|xit(|xdescribe(|xtest(|fit(|fdescribe(|pytest.mark.skip|pytest.mark.xfail|pytest.skip(|unittest.skip|@Disabled|@Ignore|t.Skip(|#[ignore]", MARKER, "|")
}
function test_path(p,    n, parts, i, name) {
    n = split(p, parts, "/")
    for (i = 1; i < n; i++)
        if (parts[i] == "tests" || parts[i] == "test" || parts[i] == "__tests__" || parts[i] == "e2e" || parts[i] == "cypress")
            return 1
    name = parts[n]
    return index(name, "test_") == 1 || index(name, "_test.") || index(name, ".test.") || index(name, ".spec.")
}
function golden(p,    n, parts, i) {
    if (p ~ /\.snap$/)
        return 1
    n = split(p, parts, "/")
    for (i = 1; i <= n; i++)
        if (parts[i] == "__snapshots__" || index(parts[i], "golden") == 1)
            return 1
    return 0
}
function runner_config(p,    name) {
    name = p
    sub(/^.*\//, "", name)
    if (p == "conftest.py" || name == "package.json" || name == "pytest.ini" || name == "tox.ini" || name == "setup.cfg")
        return 1
    return name ~ /^(jest|vitest|playwright|cypress)\.config\./
}
function flush() {
    if (path != "" && removed > 0)
        printf "tests: step %s %s: %d assertion lines removed or changed\n", STEP, path, removed
    removed = 0
}
/^diff --git / {
    flush()
    path = $0
    sub(/^.* b\//, "", path)
    is_test = test_path(path)
    if (golden(path))
        printf "tests: step %s %s: snapshot or golden file changed\n", STEP, path
    if (runner_config(path))
        printf "tests: step %s %s: test runner config changed\n", STEP, path
    split("", SEEN)
    next
}
/^(\+\+\+|---) / { next }
is_test && /^\+/ {
    # The longest marker that the line holds names it: pytest.mark.skip,
    # not the .skip( inside it.
    best = ""
    for (i = 1; i <= count; i++) {
        rest = $0
        while ((at = index(rest, MARKER[i])) > 0) {
            before = (at > 1) ? substr(rest, at - 1, 1) : ""
            if (substr(MARKER[i], 1, 1) == "." || before !~ /[A-Za-z0-9]/) {
                if (length(MARKER[i]) > length(best))
                    best = MARKER[i]
                break
            }
            rest = substr(rest, at + length(MARKER[i]))
        }
    }
    if (best != "" && !(best in SEEN)) {
        SEEN[best] = 1
        printf "tests: step %s %s: skip marker added: %s\n", STEP, path, best
    }
    next
}
is_test && /^-/ {
    if (index($0, "assert") || index($0, "expect(") || index($0, ".should"))
        removed++
}
END { flush() }
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
npm_config_yes=false
export npm_config_yes
# Only the capture script reads the login variables (see the header comment).
unset WHYGRAPH_UI_USER WHYGRAPH_UI_PASSWORD
# A setup or dry-run job loses the strip list (see the header comment).
if [ -f "$1/strip" ]; then
    for name in $(env | sed -n "s/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p"); do
        case $name in
            *_API_KEY | AWS_SECRET_ACCESS_KEY | AWS_SESSION_TOKEN | CLAUDE_CODE_OAUTH_TOKEN | WHYGRAPH_*)
                unset "$name" ;;
        esac
    done
fi
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
        # A background process that a command started must not outlive it.
        if [ "$job_form" -ne 4 ] && kill -TERM -"$job" 2>/dev/null; then
            sleep 1
            kill -KILL -"$job" 2>/dev/null
        fi
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
    cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    record=$steps_dir/$step.verify
    check_leaf "$record"
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
    if ! test_first_intact "$work/plan" "$step"; then
        printf 'verify: test first edited\n'
        exit 5
    fi

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
    # The trust root (section 5.24): the caller keeps the hash for end.
    trust_file=$steps_dir/$step.trust
    check_leaf "$trust_file"
    trust_manifest "$step" >"$steps_dir/.trust.$$" || fail "cannot write $trust_file"
    mv -f "$steps_dir/.trust.$$" "$trust_file" || fail "cannot write $trust_file"
    printf 'trust: %s\n' "$(run_git hash-object --stdin <"$trust_file")"
}

cmd_end() {
    # end <plan> <N> <t0> [<blob>] [--trust <hash>]
    trust=
    count=0
    plan=
    step=
    t0=
    blob=
    while [ $# -gt 0 ]; do
        if [ "$1" = --trust ]; then
            [ $# -ge 2 ] || usage
            trust=$2
            is_object_id "$trust" || usage "not a trust hash: $trust"
            shift 2
            continue
        fi
        count=$((count + 1))
        case $count in
            1) plan=$1 ;;
            2) step=$1 ;;
            3) t0=$1 ;;
            4) blob=$1 ;;
            *) usage ;;
        esac
        shift
    done
    [ "$count" -ge 3 ] || usage
    check_step "$step"
    is_object_id "$t0" || usage "not a tree id: $t0"
    [ -z "$blob" ] || is_object_id "$blob" || usage "not a blob id: $blob"
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
            .claude/skills/whygraph-plan/* | .cursor/skills/whygraph-plan/* | .github/whygraph/* | .codex/whygraph/* | \
                .claude/skills/whygraph-ui-check/* | .cursor/skills/whygraph-ui-check/*)
                printf 'plan-tool.sh: the step changed the plan tool: %s\n' "$path" >&2
                exit 65
                ;;
        esac
    done <"$work/changed"
    if [ -n "$trust" ]; then
        trust_manifest "$step" >"$work/trust"
        if [ "$(run_git hash-object --stdin <"$work/trust")" != "$trust" ]; then
            trust_file=$steps_dir/$step.trust
            if [ -f "$trust_file" ] && [ "$(run_git hash-object --stdin <"$trust_file")" = "$trust" ]; then
                # The label is the line without its state: "- <id>", "x <id>",
                # "none", "other" or "link <target>".
                LC_ALL=C comm -3 "$trust_file" "$work/trust" | sed 's/^[[:space:]]*//' |
                    sed -E 's/ (- [^ ]+|x [^ ]+|none|other|link .*)$//' |
                    LC_ALL=C sort -u | while IFS= read -r label; do
                        printf 'trust: changed: %s\n' "$label"
                    done
            else
                printf 'trust: changed\n'
            fi
            printf 'plan-tool.sh: a trusted file changed during the step\n' >&2
            exit 65
        fi
    fi
    if ! test_first_intact "$work/plan" "$step"; then
        printf 'verify: test first edited\n'
        exit 5
    fi
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

# Run the spans of $work/spans as one job and set result to its baseline
# form: pass, "pass (no test ran)", "timeout <s>: <command>", "cannot run
# <code>: <command>" or "fail <code>: <excerpt>". $work/plan is the plan.
run_verify_result() {
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
        run_verify_result
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

    # An e2e: Check runs nothing here: the caller runs the browser test.
    raw=$(plan_field "$work/plan" "$item" Check raw | sed 's/^[ 	]*//')
    case $raw in
        e2e:*)
            e2e_path=${raw#e2e:}
            e2e_path=$(printf '%s\n' "$e2e_path" | LC_ALL=C awk '{ print $1; exit }')
            if path_is_safe "$e2e_path" && [ -n "$e2e_path" ]; then
                put_file "$record" 'exit: e2e'
                printf 'accept: e2e %s\n' "$e2e_path"
                exit 0
            fi
            ;;
    esac
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

# Read plan $1, or blob $2 when set, into $work/plan; set stem, cd to the
# repository root and check the work directory.
load_plan() {
    if [ -z "${2:-}" ]; then
        cat <"$1" >"$work/plan" || usage "cannot read $1"
    fi
    stem=$(stem_of "$1")
    cd "$root" || exit 1
    [ -z "${2:-}" ] || read_blob "$2" "$work/plan"
    check_work_dir
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
}

# Start a job that loses the strip list, unless the person kept it.
strip_secrets() {
    rm -f "$work/strip"
    [ "${WHYGRAPH_KEEP_SECRETS:-}" = 1 ] || : >"$work/strip"
}

# Run the "## Setup" commands of $work/plan as one job and write
# steps/setup. Prints "setup: <k> of <n> commands ran, exit <code>" and
# returns 0, 1 (a command failed), 3 (the deadline), 5 (a tracked or
# unignored file changed; the paths follow) or 6 (no section).
run_setup() {
    plan_field "$work/plan" "" Setup >"$work/spans"
    [ $? -ne 3 ] || {
        printf 'setup: no Setup section\n'
        return 6
    }
    setup_record=$steps_dir/setup
    check_leaf "$setup_record"
    total=$(LC_ALL=C awk 'END { print NR }' <"$work/spans")
    before=$(take_snapshot) || fail "snapshot failed"
    strip_secrets
    run_job
    after=$(take_snapshot) || fail "snapshot failed"
    rm -f "$work/strip"
    setup_code=$code
    ran=$(LC_ALL=C awk 'END { print NR }' <"$work/ran")
    start_write "$setup_record"
    {
        cat "$work/ran"
        printf 'exit: %s\n' "$setup_code"
    } >"$tmp"
    finish_write "$setup_record"
    printf 'setup: %s of %s commands ran, exit %s\n' "$ran" "$total" "$setup_code"
    setup_last=$(sed -n 's/^command: //p' "$work/ran" | tail -n 1)
    if [ "$setup_code" -eq 124 ] && [ -f "$work/timed-out" ]; then
        return 3
    fi
    [ "$setup_code" -eq 0 ] || return 1
    setup_paths=
    if [ "$before" != "$after" ]; then
        setup_paths=$(run_git -c core.quotePath=false diff --name-only --no-renames "$before" "$after") ||
            fail "git diff failed"
        printf '%s\n' "$setup_paths"
        return 5
    fi
    return 0
}

cmd_setup() {
    [ $# -eq 1 ] || [ $# -eq 2 ] || usage
    plan=$1
    blob=${2:-}
    [ -z "$blob" ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    load_plan "$plan" "$blob"
    run_setup
    exit $?
}

# format <plan> <t0> [<blob>]: run the Format command of the plan on the
# files that differ between tree <t0> and the working tree (see the header
# comment). Each path passes path_is_safe, so single quotes hold it whole.
cmd_format() {
    [ $# -eq 2 ] || [ $# -eq 3 ] || usage
    plan=$1
    t0=$2
    blob=${3:-}
    is_object_id "$t0" || usage "not a tree id: $t0"
    [ -z "$blob" ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    run_git cat-file -e "$t0^{tree}" 2>/dev/null || usage "no such tree: $t0"
    read_budget
    choose_kill_form
    # The plan path is relative to the caller: resolve it before the cd.
    plan_at=
    if [ -f "$plan" ]; then
        plan_at=$(cd "$(dirname "$plan")" 2>/dev/null && pwd -P)/${plan##*/} || plan_at=
    fi
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    load_plan "$plan" "$blob"
    root=$(pwd -P)
    fmt_command=$(plan_field "$work/plan" "" Format | head -n 1)
    [ -n "$fmt_command" ] || {
        printf 'format: no Format command\n'
        exit 6
    }
    now=$(take_snapshot) || fail "snapshot failed"
    run_git -c core.quotePath=false diff --name-only --no-renames "$t0" "$now" >"$work/paths" ||
        fail "git diff failed"
    count=0
    unsafe=0
    while IFS= read -r path; do
        case $path in
            .whygraph | .whygraph/*) continue ;;
        esac
        [ "$root/$path" != "$plan_at" ] || continue
        [ -f "$path" ] && [ ! -h "$path" ] || continue
        if ! path_is_safe "$path"; then
            unsafe=$((unsafe + 1))
            continue
        fi
        fmt_command="$fmt_command '$path'"
        count=$((count + 1))
    done <"$work/paths"
    [ "$unsafe" -eq 0 ] || printf 'format: unsafe paths left out: %s\n' "$unsafe"
    if [ "$count" -eq 0 ]; then
        printf 'format: 0 files, exit 0\n'
        exit 0
    fi
    printf '%s\n' "$fmt_command" >"$work/spans"
    strip_secrets
    run_job
    rm -f "$work/strip"
    printf 'format: %s files, exit %s\n' "$count" "$code"
    if [ "$code" -eq 124 ] && [ -f "$work/timed-out" ]; then
        exit 3
    fi
    [ "$code" -eq 0 ] || exit 1
    exit 0
}

# The ready test on record $1 (a baseline or a dry-run record) for plan
# file $2. With $3 "resume", a run that a .done marker or a logged pass
# shows is not fresh prints "ready: resumed". For the last result line of
# each step: a "fail" whose Verify waits for an earlier step prints "ready:
# step <N> waits for an earlier step"; any other prints "not ready: step
# <N>: <excerpt>". Returns 1 when it printed a "not ready" line.
ready_check() {
    scan=$(plan_field "$2" "" "")
    if [ "${3:-}" = resume ]; then
        while read -r kind number; do
            [ "$kind" = step ] || continue
            if [ -f "$steps_dir/$number.done" ] || logged_pass "$number" ||
                { printf '%s\n' "$scan" | grep -qx "logged $number" &&
                    [ "$(record_exit "$steps_dir/$number.verify")" = 0 ]; }; then
                printf 'ready: resumed\n'
                return 0
            fi
        done <<EOF
$scan
EOF
    fi
    [ -f "$1" ] || {
        printf 'ready: no baseline\n'
        return 0
    }
    LC_ALL=C awk '
        $1 == "step" && $2 ~ /^[0-9]+$/ && index($0, ": ") > 0 {
            if (!($2 in last))
                order[++n] = $2
            last[$2] = substr($0, index($0, ": ") + 2)
        }
        END { for (i = 1; i <= n; i++) print order[i] "\t" last[order[i]] }
    ' <"$1" >"$work/last"
    not_ready=0
    while IFS='	' read -r number result; do
        case $result in
            'fail '*) ;;
            *) continue ;;
        esac
        if waits_for_earlier "$2" "$number"; then
            printf 'ready: step %s waits for an earlier step\n' "$number"
        elif browser_test_of "$2" "$number" && [ "$bt_kind" = red ]; then
            printf 'ready: step %s writes its browser test\n' "$number"
        else
            printf 'not ready: step %s: %s\n' "$number" "${result#fail *: }"
            not_ready=1
        fi
    done <"$work/last"
    [ "$not_ready" -eq 0 ] || return 1
    printf 'ready: ok\n'
    return 0
}

cmd_ready() {
    [ $# -eq 1 ] || [ $# -eq 2 ] || usage
    plan=$1
    blob=${2:-}
    [ -z "$blob" ] || is_object_id "$blob" || usage "not a blob id: $blob"
    [ -n "$blob" ] || [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    load_plan "$plan" "$blob"
    ready_check "$steps_dir/baseline" "$work/plan" resume
    exit $?
}

# 0 when a word of step $2's Verify spans in plan file $1 is a path that the
# Files of an earlier step name, or a directory that holds such a path: the
# Verify waits for an earlier step (R17). A "::<test id>" suffix and a
# leading ./ are cut from each word.
waits_for_earlier() {
    files=$(plan_field "$1" "" "" | while read -r kind number; do
        [ "$kind" = step ] || continue
        [ "$number" != "$2" ] || break
        plan_field "$1" "$number" Files
        printf ','
    done)
    entries=$(FILES=$files LIST=1 LC_ALL=C awk "$AWK_OUTSIDE" </dev/null |
        LC_ALL=C awk -F '\t' '$1 != "-" { print $1 }')
    [ -n "$entries" ] || return 1
    plan_field "$1" "$2" Verify | LC_ALL=C tr ' \t' '\n\n' | ENTRIES=$entries LC_ALL=C awk '
        BEGIN { count = split(ENVIRON["ENTRIES"], ENTRY, "\n") }
        {
            word = $0
            sub(/::.*$/, "", word)
            sub(/^\.\//, "", word)
            if (word == "")
                next
            dir = word
            sub(/\/+$/, "", dir)
            for (i = 1; i <= count; i++) {
                e = ENTRY[i]
                if (word == e || (substr(e, length(e)) == "/" && index(word, e) == 1) ||
                    (dir != "" && index(e, dir "/") == 1)) {
                    found = 1
                    exit
                }
            }
        }
        END { exit found ? 0 : 1 }
    '
}

# The exit value of record $1: the first "exit:" line before the output, or
# "missing" when there is no record.
record_exit() {
    if [ ! -f "$1" ]; then
        printf 'missing\n'
        return
    fi
    LC_ALL=C awk '/^--- / { exit } /^exit: / { print substr($0, 7); found = 1; exit } END { if (!found) print "missing" }' <"$1"
}

# 0 when the output part of record $1 holds a phrase of AWK_NO_TEST.
ran_no_test() {
    LC_ALL=C awk 'out { print } /^--- / { out = 1 }' <"$1" | LC_ALL=C awk "$AWK_NO_TEST"
}

# ---- the browser test (plan 56 sections 5.11, 5.13, 5.24, 5.25).

# The capture script: in the tool's own directory (VS Code, Codex), else in
# ../whygraph-ui-check/ (Claude Code, Cursor), with no symlink on its path.
# Prints its absolute path, or returns 1.
find_capture() {
    for candidate in "$tool_dir/capture.mjs" "$tool_dir/../whygraph-ui-check/capture.mjs"; do
        [ -n "$tool_dir" ] && [ -f "$candidate" ] || continue
        dir=$(cd "$(dirname "$candidate")" && pwd -P) || continue
        [ ! -h "$candidate" ] && [ ! -h "$(dirname "$candidate")" ] || continue
        printf '%s/capture.mjs\n' "$dir"
        return 0
    done
    return 1
}

# Set bt_path and bt_kind from step $2's Browser test line in plan file $1.
# Returns 1 when the step has none or the line is malformed.
browser_test_of() {
    bt=$(plan_field "$1" "$2" "Browser test" | LC_ALL=C tr -d '`')
    bt_kind=${bt##* - }
    bt_path=${bt% - *}
    bt_path=$(printf '%s' "$bt_path" | LC_ALL=C awk '{ sub(/^[ \t]+/, ""); sub(/[ \t]+$/, ""); print }')
    case $bt_kind in
        red | green) ;;
        *) return 1 ;;
    esac
    [ -n "$bt_path" ] && [ "$bt_path" != "$bt" ] && path_is_safe "$bt_path"
}

# 0 when the whole browser test file of every red step up to $2 is as its
# red run saw it. A difference prints the finding on stderr.
# Its loop variable is its own: a caller's loop may use number.
browser_intact() {
    for bi_step in $(plan_field "$1" "" "" | sed -n 's/^step //p'); do
        [ "$bi_step" -le "$2" ] || break
        browser_test_of "$1" "$bi_step" && [ "$bt_kind" = red ] || continue
        snap=.whygraph/plans/$stem/testfirst/$bi_step/${bt_path##*/}
        [ -f "$snap" ] || continue
        if [ ! -f "$bt_path" ] || [ -h "$bt_path" ] || ! cmp -s "$snap" "$bt_path"; then
            printf 'the browser test was edited: %s\n' "$bt_path" >&2
            return 1
        fi
    done
    return 0
}

# Run the capture script's --e2e mode on $1 with --out $2, as one bounded
# job without the strip list. Sets code and the job's output files.
run_capture_e2e() {
    capture=$(find_capture) || return 1
    printf "node '%s' --e2e %s --out %s\n" "$capture" "$1" "$2" >"$work/spans"
    saved_budget=$budget
    budget=580
    strip_secrets
    run_job
    rm -f "$work/strip"
    budget=$saved_budget
    return 0
}

# The trust list of step $2 (section 5.24): the plan tool's directory, the
# capture script, the UI profile and saved login, and the red records and
# snapshots of every step before $2. One sorted line per path.
trust_manifest() {
    {
        for f in "$tool_dir"/*; do
            [ -f "$f" ] || [ -h "$f" ] || continue
            state_line "tool ${f##*/}" "$f"
        done
        capture=$(find_capture) && state_line capture "$capture"
        state_line profile .whygraph/profiles/ui.md
        state_line login .whygraph/profiles/ui-auth.json
        for record in "$steps_dir"/*.red; do
            [ -e "$record" ] || [ -h "$record" ] || continue
            number=${record##*/}
            number=${number%.red}
            case $number in '' | *[!0123456789]*) continue ;; esac
            [ "$number" -lt "$1" ] || continue
            state_line "red $number" "$record"
        done
        for snap in ".whygraph/plans/$stem/testfirst"/*/*; do
            [ -e "$snap" ] || [ -h "$snap" ] || continue
            number=${snap%/*}
            number=${number##*/}
            case $number in '' | *[!0123456789]*) continue ;; esac
            [ "$number" -lt "$1" ] || continue
            state_line "snapshot $number ${snap##*/}" "$snap"
        done
    } | LC_ALL=C sort
}

# smoke <out path> <test file>: run one existing browser test at plan time.
cmd_smoke() {
    [ $# -eq 2 ] || usage
    plan=$1
    test_file=$2
    path_is_safe "$test_file" || usage "not a plain relative path: $test_file"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    choose_kill_form
    budget=580
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    mkdir -p "$steps_dir" || fail "cannot create $steps_dir"
    record=$steps_dir/smoke
    check_leaf "$record"
    if ! run_capture_e2e "$test_file" ".whygraph/plans/$stem/e2e/smoke"; then
        line='smoke: not available: no capture script'
        code=1
        : >"$work/output"
    else
        last=$(LC_ALL=C awk 'NF { l = $0 } END { print l }' <"$work/output")
        case $code in
            0) line='smoke: pass' ;;
            2) line="smoke: fail: $last" ;;
            5) line='smoke: timeout' ;;
            *) line="smoke: not available: $last" ;;
        esac
    fi
    start_write "$record"
    {
        printf 'test: %s\n' "$test_file"
        printf 'exit: %s\n' "$code"
        printf 'result: %s\n' "${line#smoke: }"
        printf '%s\n' '--- last 40 lines of output'
        tail -n 40 "$work/output"
    } >"$tmp"
    finish_write "$record"
    printf '%s\n' "$line"
    exit 0
}

# ---- red and the Test first guard (plan 56 sections 5.34 and 5.35).

# Set tf_file and tf_name from step $2's Test first in plan file $1, as
# "<test file>::<test name>". Returns 1 when the step names no test.
test_first_of() {
    tf=$(plan_field "$1" "$2" "Test first")
    names_test_first "$tf" || return 1
    word=$(printf '%s\n' "$tf" | LC_ALL=C awk '{ print $1; exit }' | LC_ALL=C tr -d '`"*')
    word=${word%[.,;:]}
    case $word in
        *::*) ;;
        *) return 2 ;;
    esac
    tf_file=${word%::*}
    tf_name=${word##*::}
    [ -n "$tf_file" ] && [ -n "$tf_name" ] || return 2
}

# 0 when file $1 holds the test name $2 with no letter, digit or _ after it.
holds_test() {
    [ -n "$(NAME=$2 LC_ALL=C awk "$AWK_BLOCK" <"$1")" ]
}

# The snapshot of step $1's Test first file, under the plan's work directory.
snapshot_of() {
    printf '.whygraph/plans/%s/testfirst/%s/%s\n' "$stem" "$1" "${tf_file##*/}"
}

# 0 when path $1 is a test file as section 5.36 names one.
is_test_path() {
    case /$1/ in
        */tests/* | */test/* | */__tests__/* | */e2e/* | */cypress/*) return 0 ;;
    esac
    case ${1##*/} in
        test_* | *_test.* | *.test.* | *.spec.*) return 0 ;;
    esac
    return 1
}

# The guard. 0 when step $2 of plan file $1 has no Test first, has no
# snapshot, or its test block is as it was at the red run; else print the
# finding on stderr and return 1. An added line that holds a skip marker
# anywhere in the file counts as an edit too.
test_first_intact() {
    browser_intact "$1" "$2" || return 1
    test_first_of "$1" "$2" || return 0
    snap=$(snapshot_of "$2")
    [ -f "$snap" ] || return 0
    if [ ! -f "$tf_file" ] || [ -h "$tf_file" ]; then
        printf 'the Test first test was edited: %s\n' "$tf_file" >&2
        return 1
    fi
    NAME=$tf_name LC_ALL=C awk "$AWK_BLOCK" <"$snap" >"$work/block-before"
    NAME=$tf_name LC_ALL=C awk "$AWK_BLOCK" <"$tf_file" >"$work/block-after"
    LC_ALL=C awk "$AWK_MARKERS" <"$snap" | sort -u >"$work/markers-before"
    added=$(LC_ALL=C awk "$AWK_MARKERS" <"$tf_file" | sort -u | comm -13 "$work/markers-before" -)
    if [ ! -s "$work/block-after" ] || ! cmp -s "$work/block-before" "$work/block-after" || [ -n "$added" ]; then
        printf 'the Test first test was edited: %s\n' "$tf_file" >&2
        return 1
    fi
    return 0
}

# 0 when a step of plan file $1 has a Test first whose test file holds the
# named test now, before the change.
test_first_exists() {
    for tfe_step in $(plan_field "$1" "" "" | sed -n 's/^step //p'); do
        test_first_of "$1" "$tfe_step" || continue
        [ -f "$tf_file" ] && [ ! -h "$tf_file" ] && holds_test "$tf_file" "$tf_name" && return 0
    done
    return 1
}

cmd_red() {
    [ $# -ge 2 ] && [ $# -le 4 ] || usage
    plan=$1
    step=$2
    shift 2
    again=
    blob=
    if [ $# -gt 0 ] && [ "$1" = again ]; then
        again=1
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
    plan_path=$plan
    load_plan "$plan" "$blob"
    plan_field "$work/plan" "$step" Verify >"$work/spans"
    [ $? -ne 3 ] || usage "no step $step in $plan"

    # 1. The Test first and a literal Verify, or a red Browser test line,
    # whose test is the whole file and whose red run is the capture script.
    browser=
    if browser_test_of "$work/plan" "$step" && [ "$bt_kind" = red ]; then
        if names_test_first "$(plan_field "$work/plan" "$step" "Test first")"; then
            printf 'red: Browser test red and Test first on one step\n'
            exit 6
        fi
        browser=1
        tf_file=$bt_path
        tf_name=
    else
        test_first_of "$work/plan" "$step"
        case $? in
            1)
                printf 'red: step %s has no Test first\n' "$step"
                exit 6
                ;;
            2)
                printf 'red: Test first is not <test file>::<test name>\n'
                exit 5
                ;;
        esac
        if [ ! -s "$work/spans" ]; then
            printf 'red: Verify is not literal\n'
            exit 6
        fi
    fi

    # 2. The test file.
    path_is_safe "$tf_file" || {
        printf 'red: the test file is not a plain relative path: %s\n' "$tf_file"
        exit 5
    }
    if has_symlink_part "$tf_file"; then
        printf 'red: the test file has a symlink on its path: %s\n' "$tf_file"
        exit 5
    fi
    [ -f "$tf_file" ] || {
        printf 'red: the test file does not exist: %s\n' "$tf_file"
        exit 5
    }
    [ -n "$browser" ] || holds_test "$tf_file" "$tf_name" || {
        printf 'red: %s does not hold %s\n' "$tf_file" "$tf_name"
        exit 5
    }

    record=$steps_dir/$step.red
    check_leaf "$record"
    snap=$(snapshot_of "$step")
    for part in ".whygraph/plans/$stem/testfirst" ".whygraph/plans/$stem/testfirst/$step"; do
        [ ! -h "$part" ] || refuse "$part is a symlink"
    done
    check_leaf "$snap"

    # 3. The skip rule.
    if [ -z "$again" ]; then
        if [ -f "$record" ] && grep -q '^result: red' "$record"; then
            printf 'red: recorded\n'
            exit 0
        fi
        if [ ! -f "$record" ] && [ -f "$snap" ]; then
            printf 'red: recorded (snapshot only)\n'
            exit 0
        fi
    fi

    # 3a. Unchanged code: only test files and the plan may differ from t0.
    tree=$(take_snapshot) || fail "snapshot failed"
    changed=
    t0=$(cat "$steps_dir/$step.t0" 2>/dev/null) || t0=
    if is_object_id "$t0" && run_git cat-file -e "$t0^{tree}" 2>/dev/null; then
        plan_rel=${plan_path#./}
        # A browser step writes the helpers its Files name, never feature code.
        own_files=
        [ -z "$browser" ] || own_files=$(plan_field "$work/plan" "$step" Files)
        changed=$(run_git -c core.quotePath=false diff --name-only --no-renames "$t0" "$tree" |
            while IFS= read -r path; do
                [ "$path" = "$tf_file" ] || [ "$path" = "$plan_rel" ] || is_test_path "$path" ||
                    { [ -n "$browser" ] && [ -z "$(named_outside "$path" "$own_files")" ]; } ||
                    printf '%s\n' "$path"
            done)
    fi
    if [ -n "$changed" ] && [ -n "$again" ]; then
        printf 'red: the code changed since the step began\n'
        exit 5
    fi

    # 4. The snapshot of the test file.
    mkdir -p "${snap%/*}" || fail "cannot create ${snap%/*}"
    start_write "$snap"
    cat <"$tf_file" >"$tmp" || {
        rm -f "$tmp"
        fail "cannot read $tf_file"
    }
    finish_write "$snap"

    if [ -n "$changed" ]; then
        result='not run: the code changed before the red run'
        start_write "$record"
        {
            if [ -n "$browser" ]; then
                printf 'test: %s\n' "$tf_file"
                printf 'kind: browser\n'
            else
                printf 'test: %s::%s\n' "$tf_file" "$tf_name"
            fi
            printf 'tree: %s\n' "$tree"
            printf 'result: %s\n' "$result"
        } >"$tmp"
        finish_write "$record"
        printf 'red: %s\n' "$result"
        exit 0
    fi

    # 5. The red run. It never writes <N>.verify.
    exit_of=0
    if [ -n "$browser" ]; then
        code=
        if run_capture_e2e "$tf_file" ".whygraph/plans/$stem/e2e/$step"; then
            last=$(LC_ALL=C awk 'NF { l = $0 } END { print l }' <"$work/output")
            case $code in
                0)
                    result=passed
                    exit_of=1
                    ;;
                5) result='not run: timed out' ;;
                2)
                    if LC_ALL=C awk "$AWK_NO_TEST" <"$work/output" ||
                        LC_ALL=C grep -qiE 'no spec files were found|error preparing this test file' "$work/output"; then
                        result='red (did not run)'
                    else
                        result=red
                    fi
                    ;;
                *) result="not run: $last" ;;
            esac
        else
            result='not run: no capture script'
            code=none
            : >"$work/ran"
            : >"$work/output"
        fi
    else
        run_job
    fi
    # 6. The result: the recorded runner output in tests/fixtures/red/ fixed
    # this order.
    if [ -n "$browser" ]; then
        :
    elif [ "$code" -eq 0 ]; then
        if LC_ALL=C awk "$AWK_NO_TEST" <"$work/output" || LC_ALL=C awk "$AWK_NOT_COLLECTED" <"$work/output"; then
            result='red (did not run)'
        else
            result=passed
            exit_of=1
        fi
    elif [ "$code" -eq 124 ] && [ -f "$work/timed-out" ]; then
        result=timeout
        exit_of=124
    elif { [ "$code" -eq 126 ] || [ "$code" -eq 127 ]; } &&
        cannot_run "$(sed -n 's/^command: //p' "$work/ran" | tail -n 1)" "$(all_files "$work/plan")"; then
        result='cannot run'
        exit_of=4
    elif LC_ALL=C awk "$AWK_NOT_COLLECTED" <"$work/output"; then
        result='red (did not run)'
    elif NAME=$tf_name LC_ALL=C awk "$AWK_FAILURE" <"$work/output"; then
        result=red
    elif LC_ALL=C awk "$AWK_NO_TEST" <"$work/output" && ! NAME= LC_ALL=C awk "$AWK_FAILURE" <"$work/output"; then
        result='red (did not run)'
    else
        result='red (unnamed)'
    fi

    # 7. The record.
    start_write "$record"
    {
        if [ -n "$browser" ]; then
            printf 'test: %s\n' "$tf_file"
            printf 'kind: browser\n'
        else
            printf 'test: %s::%s\n' "$tf_file" "$tf_name"
        fi
        cat "$work/ran"
        printf 'exit: %s\n' "$code"
        printf 'tree: %s\n' "$tree"
        printf 'result: %s\n' "$result"
        printf '%s\n' '--- last 40 lines of output'
        tail -n 40 "$work/output"
    } >"$tmp"
    finish_write "$record"
    printf 'red: %s\n' "$result"
    exit "$exit_of"
}

# ---- dryrun: the dry run of a plan at plan time (see the header comment).

# 0 when unit $1 is marked done in the dry-run record.
dry_done() {
    grep -qxF "done: $1" "$record"
}

dry_mark() {
    printf 'done: %s\n' "$1" >>"$record" || fail "cannot append to $record"
}

# Run $work/spans within the time left in this call, without the strip
# list's variables, and set result as baseline does. Returns 2 when the
# call must end first: too little time left, or the end of the call cut the
# command short. A command cut by its own deadline is a result.
dry_run_unit() {
    left=$((deadline - ($(date +%s) - started)))
    # The first command of a call gets the whole deadline: the 20 s between
    # the 580 s cap and the 600 s shell limit cover the call's own work. A
    # later command needs 5 s and gets the time left.
    if [ "$ran_here" -eq 0 ]; then
        left=$deadline
    elif [ "$left" -lt 5 ]; then
        return 2
    fi
    budget=$left
    strip_secrets
    run_verify_result
    rm -f "$work/strip"
    budget=$deadline
    ran_here=$((ran_here + 1))
    case $result in
        'timeout '*) [ "$left" -ge "$deadline" ] || return 2 ;;
    esac
    cat "$work/ran" >>"$record" || fail "cannot append to $record"
    return 0
}

# The baseline of step $1 into the record, as unit $2. $3 "again" runs a
# failing Verify once more: a pass makes it "unstable: <first excerpt>".
dry_step() {
    plan_field "$work/plan" "$1" Verify >"$work/spans"
    key=$(plan_field "$work/plan" "$1" Verify raw | run_git hash-object --stdin) ||
        fail "git hash-object failed"
    if names_test_first "$(plan_field "$work/plan" "$1" "Test first")"; then
        result='test first'
    elif [ ! -s "$work/spans" ]; then
        result='not literal'
    else
        dry_run_unit || return 2
        if [ "${3:-}" = again ] && [ "$result" = pass ]; then
            result="unstable: $first"
        fi
    fi
    printf 'step %s %s: %s\n' "$1" "$key" "$result" >>"$record" || fail "cannot append to $record"
    dry_mark "$2"
}

# The last result line of each step in the record, as "<N> TAB <line
# number> TAB <result>", in step order.
dry_last() {
    LC_ALL=C awk '
        $1 == "step" && $2 ~ /^[0-9]+$/ && index($0, ": ") > 0 {
            if (!($2 in last))
                order[++n] = $2
            last[$2] = substr($0, index($0, ": ") + 2)
            at[$2] = NR
        }
        END { for (i = 1; i <= n; i++) print order[i] "\t" at[order[i]] "\t" last[order[i]] }
    ' <"$record"
}

# An excerpt fit for one finding line: no "|", at most 80 bytes.
dry_excerpt() {
    printf '%s\n' "$1" | LC_ALL=C tr '|' '/' |
        LC_ALL=C awk '{ s = substr($0, 1, 80) } END { print (s ~ /[^ \t]/) ? s : "no output" }'
}

dry_finding() {
    findings=$((findings + 1))
    printf '%s\n' "- F$findings | blocker | $1 | $2 | evidence: $record:$3 | fix: $4" >>"$work/findings"
}

# The paths that differ between trees $1 and $2, joined by ", ".
dry_paths() {
    [ "$1" != "$2" ] || return 0
    run_git -c core.quotePath=false diff --name-only --no-renames "$1" "$2" || fail "git diff failed"
}

# End the call: print the block, mark the record done, exit 0.
dry_finish() {
    if [ -z "$not_checked" ] && ! dry_done finish; then
        tree1=$(take_snapshot) || fail "snapshot failed"
        tree0=$(sed -n 's/^tree: //p' "$record" | head -n 1)
        pre=$(sed -n 's/^setup trees: \([^ ]*\) .*/\1/p' "$record" | tail -n 1)
        post=$(sed -n 's/^setup trees: [^ ]* //p' "$record" | tail -n 1)
        if [ -n "$pre" ]; then
            paths=$({ dry_paths "$tree0" "$pre"; dry_paths "$post" "$tree1"; } | sort -u)
        else
            paths=$(dry_paths "$tree0" "$tree1")
        fi
        [ -z "$paths" ] || printf 'changed: %s\n' "$(printf '%s\n' "$paths" | LC_ALL=C awk 'NR > 1 { printf ", " } { printf "%s", $0 }')" >>"$record"
        dry_mark finish
    fi
    findings=0
    : >"$work/findings"
    grep -n '^probe: ' "$record" | while IFS= read -r line; do
        at=${line%%:*}
        rest=${line#*:probe: }
        where=${rest%%: missing *}
        word=${rest#*: missing }
        case $where in
            step*) printf '%s\t%s\tVerify cannot run: %s is missing\tInstall %s, or use a Verify whose command this machine has.\n' "$where" "$at" "$word" "$word" ;;
            *) printf 'acceptance\t%s\tCheck cannot run: %s is missing\tInstall %s, or use a Check whose command this machine has.\n' "$at" "$word" "$word" ;;
        esac
    done >"$work/lines"
    dry_last >"$work/last"
    while IFS='	' read -r number at result; do
        case $result in
            'fail '*)
                waits_for_earlier "$work/plan" "$number" && continue
                { browser_test_of "$work/plan" "$number" && [ "$bt_kind" = red ]; } && continue
                printf 'step %s\t%s\tVerify fails before the change: %s\tUse a Verify that passes before the change, or fix the environment.\n' \
                    "$number" "$at" "$(dry_excerpt "${result#fail *: }")"
                ;;
            'pass (no test ran)')
                waits_for_earlier "$work/plan" "$number" && continue
                printf 'step %s\t%s\tVerify ran no test: %s\tPoint the Verify at a test that exists before the change.\n' \
                    "$number" "$at" "$(dry_excerpt "$(plan_field "$work/plan" "$number" Verify | head -n 1)")"
                ;;
            'unstable: '*)
                printf 'step %s\t%s\tVerify is unstable: failed, then passed: %s\tMake the test stable, or point the Verify at a stable test.\n' \
                    "$number" "$at" "$(dry_excerpt "${result#unstable: }")"
                ;;
            'timeout '*)
                seconds=${result#timeout }
                printf 'step %s\t%s\tVerify did not finish in %s s\tUse a faster Verify, or raise WHYGRAPH_VERIFY_TIMEOUT, at most 580.\n' \
                    "$number" "$at" "${seconds%%:*}"
                ;;
        esac
    done <"$work/last" >>"$work/lines"
    at=$(grep -n '^setup: exit ' "$record" | tail -n 1)
    if [ -n "$at" ]; then
        setup_code=${at##*setup: exit }
        if [ "$setup_code" -ne 0 ]; then
            last_setup=$(sed -n 's/^setup command: //p' "$record" | tail -n 1)
            printf 'plan\t%s\tSetup failed: %s (exit %s)\tFix the Setup command, or remove it from ## Setup.\n' \
                "${at%%:*}" "$(dry_excerpt "$last_setup")" "$setup_code" >>"$work/lines"
        fi
    fi
    at=$(grep -n '^setup changed: ' "$record" | tail -n 1)
    [ -z "$at" ] || printf 'plan\t%s\tSetup changed tracked files: %s\tMake each Setup command write only files that git ignores.\n' \
        "${at%%:*}" "$(dry_excerpt "${at#*setup changed: }")" >>"$work/lines"
    at=$(grep -n '^acceptance 1: ' "$record" | tail -n 1)
    # A Test first test that exists already fails the full test command
    # before the change by design: no finding then.
    case ${at#*acceptance 1: } in
        'fail '*) ! test_first_exists "$work/plan" || at= ;;
    esac
    case ${at#*acceptance 1: } in
        'fail '*)
            printf 'acceptance\t%s\tThe full test command fails before the change: %s\tFix the environment, or name the failing tests under Risks.\n' \
                "${at%%:*}" "$(dry_excerpt "${at#*acceptance 1: fail *: }")" >>"$work/lines"
            ;;
        'timeout '*)
            seconds=${at#*acceptance 1: timeout }
            printf 'acceptance\t%s\tVerify did not finish in %s s\tUse a faster full test command, or raise WHYGRAPH_VERIFY_TIMEOUT.\n' \
                "${at%%:*}" "${seconds%%:*}" >>"$work/lines"
            ;;
    esac
    at=$(grep -n '^smoke: ' "$record" | tail -n 1)
    if [ -n "$at" ]; then
        smoke_line=${at#*smoke: }
        smoke_test=${smoke_line%% *}
        case ${smoke_line#* } in
            fail*) printf 'plan\t%s\tAn existing browser test fails before the change: %s\tFix the database, seed data or login the test needs, or plan with --no-e2e.\n' \
                "${at%%:*}" "$smoke_test" >>"$work/lines" ;;
            timeout) printf 'plan\t%s\tAn existing browser test did not finish in %s s: %s\tMake the test finish, or plan with --no-e2e.\n' \
                "${at%%:*}" "$([ "$deadline" -gt 500 ] && echo 500 || echo "$deadline")" "$smoke_test" >>"$work/lines" ;;
        esac
    fi
    at=$(grep -n '^changed: ' "$record" | tail -n 1)
    [ -z "$at" ] || printf 'plan\t%s\tA Verify changed the working tree: %s\tMake each Verify write only files that git ignores.\n' \
        "${at%%:*}" "$(dry_excerpt "${at#*changed: }")" >>"$work/lines"
    while IFS='	' read -r where at defect fix; do
        dry_finding "$where" "$defect" "$at" "$fix"
    done <"$work/lines"
    if [ -s "$work/findings" ]; then
        printf 'REVIEW: dry-run\n'
        cat "$work/findings"
    else
        printf 'REVIEW: dry-run none\n'
    fi
    commands=$(grep -cE '^(command|setup command): ' "$record")
    seconds=$(($(sed -n 's/^call [0-9]*: [a-z]* \([0-9]*\)$/\1/p' "$record" | LC_ALL=C awk '{ s += $1 } END { print s + 0 }') + $(date +%s) - started))
    if ! plan_field "$work/plan" "" Setup >/dev/null; then
        setup_state='none in plan'
    elif [ -z "$(grep '^setup: exit ' "$record")" ]; then
        setup_state='not needed'
    elif grep -qx 'setup: exit 0' "$record" && ! grep -q '^setup changed: ' "$record"; then
        setup_state=ran
    else
        setup_state="failed $(sed -n 's/^setup: exit //p' "$record" | tail -n 1)"
        ! grep -q '^setup changed: ' "$record" || setup_state='failed 5'
    fi
    printf '%s\n' "- ran: $commands commands in $seconds s; setup: $setup_state; record: $record"
    [ -z "$not_checked" ] || printf '%s\n' "- not checked: $not_checked (dry-run budget)"
    printf 'call %s: done %s\n' "$calls" "$(($(date +%s) - started))" >>"$record"
    exit 0
}

# The call ends with work left: on the sixth call, finish with the rest
# under "- not checked:", else record it and exit 2.
dry_more() {
    if [ "$calls" -ge 6 ]; then
        not_checked=$(
            for n in $steps; do dry_done "step $n" || printf 'step %s\n' "$n"; done
            [ -z "$accept_spans" ] || dry_done acceptance || printf 'acceptance\n'
        )
        not_checked=$(printf '%s\n' "$not_checked" | LC_ALL=C awk '
            /^step / { s = s (s == "" ? "steps " : ", ") $2; next }
            /./ { a = "acceptance" }
            END { if (s != "" && a != "") print s ", " a; else print s a }
        ')
        [ -n "$not_checked" ] || not_checked='later commands'
        dry_finish
    fi
    done_units=$(grep -cE '^done: (step|again|after|setup|acceptance)' "$record")
    total_units=$((step_count + $([ -n "$accept_spans" ] && printf 1 || printf 0) + $(grep -cE '^done: (again|after|setup)' "$record")))
    printf 'call %s: more %s\n' "$calls" "$(($(date +%s) - started))" >>"$record"
    printf 'dryrun: more, %s of %s commands done\n' "$done_units" "$total_units"
    exit 2
}

cmd_dryrun() {
    [ $# -eq 1 ] || usage
    plan=$1
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    read_budget
    choose_kill_form
    deadline=$budget
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    load_plan "$plan" ""
    record=$steps_dir/dryrun
    check_leaf "$record"
    id=$(run_git hash-object --no-filters -- "$work/plan") || fail "git hash-object failed"
    started=$(date +%s)
    ran_here=0
    not_checked=
    # Go on only after a call that exited 2 for the same plan.
    calls=0
    if [ -f "$record" ] && [ "$(sed -n 1p "$record")" = "plan: $id" ]; then
        case $(grep '^call [0-9]*: ' "$record" | tail -n 1) in
            call*': more '*)
                calls=$(grep '^call [0-9]*: ' "$record" | tail -n 1 | sed 's/^call \([0-9]*\):.*/\1/')
                ;;
        esac
    fi
    if [ "$calls" -eq 0 ]; then
        start_write "$record"
        printf 'plan: %s\n' "$id" >"$tmp"
        finish_write "$record"
        tree0=$(take_snapshot) || fail "snapshot failed"
        printf 'tree: %s\n' "$tree0" >>"$record"
    fi
    calls=$((calls + 1))
    printf 'call %s\n' "$calls" >>"$record"
    scan=$(plan_field "$work/plan" "" "")
    steps=$(printf '%s\n' "$scan" | sed -n 's/^step //p')
    step_count=$(printf '%s\n' "$steps" | LC_ALL=C awk 'NF { n++ } END { print n + 0 }')
    files=$(all_files "$work/plan")
    accept_spans=
    if printf '%s\n' "$scan" | grep -qx 'item 1'; then
        accept_spans=$(plan_field "$work/plan" 1 Check)
    fi

    # 1. The checks of probe.
    if ! dry_done probe; then
        while read -r kind number; do
            case $kind in
                step) where="step $number" field=Verify ;;
                item) where="acceptance $number" field=Check ;;
                *) continue ;;
            esac
            plan_field "$work/plan" "$number" "$field" | while IFS= read -r span; do
                probe_misses "$span" "$files" scripts | while read -r k word; do
                    printf 'probe: %s: missing %s\n' "$where" "$word"
                done
            done
        done <<EOF >>"$record"
$scan
EOF
        dry_mark probe
    fi

    # 3. The baseline of every step.
    for n in $steps; do
        dry_done "step $n" && continue
        dry_step "$n" "step $n" || dry_more
    done

    # 4. A failing Verify runs once more.
    dry_last | while IFS='	' read -r number at result; do
        case $result in 'fail '*) printf '%s\t%s\n' "$number" "${result#fail *: }" ;; esac
    done >"$work/failing"
    while IFS='	' read -r number first; do
        dry_done "again $number" && continue
        dry_step "$number" "again $number" again || dry_more
    done <"$work/failing"

    # 5. The ready test, then Setup once and the steps again.
    ready_check "$record" "$work/plan" >"$work/ready"
    sed -n 's/^not ready: step \([0-9]*\): .*/\1/p' "$work/ready" >"$work/not-ready"
    if [ -s "$work/not-ready" ] && plan_field "$work/plan" "" Setup >/dev/null && ! dry_done setup; then
        left=$((deadline - ($(date +%s) - started)))
        if [ "$ran_here" -eq 0 ]; then
            left=$deadline
        elif [ "$left" -lt 5 ]; then
            dry_more
        fi
        budget=$left
        run_setup >"$work/setup-out"
        setup_code=$?
        budget=$deadline
        ran_here=$((ran_here + 1))
        if [ "$setup_code" -eq 3 ] && [ "$left" -lt "$deadline" ]; then
            dry_more
        fi
        sed 's/^command: /setup command: /' "$work/ran" >>"$record"
        if [ "$setup_code" -eq 5 ]; then
            printf 'setup: exit 0\n' >>"$record"
            printf 'setup changed: %s\n' "$(printf '%s\n' "$setup_paths" | LC_ALL=C awk 'NR > 1 { printf ", " } { printf "%s", $0 }')" >>"$record"
        else
            printf 'setup: exit %s\n' "$setup_code" >>"$record"
        fi
        printf 'setup trees: %s %s\n' "${before:-none}" "${after:-none}" >>"$record"
        dry_mark setup
    fi
    if dry_done setup; then
        while read -r number; do
            dry_done "after $number" && continue
            dry_step "$number" "after $number" || dry_more
        done <"$work/not-ready"
    fi

    # 6. Acceptance item 1, in a call of its own.
    if [ -n "$accept_spans" ] && ! dry_done acceptance; then
        [ "$ran_here" -eq 0 ] || dry_more
        printf '%s\n' "$accept_spans" >"$work/spans"
        dry_run_unit || dry_more
        printf 'acceptance 1: %s\n' "$result" >>"$record"
        dry_mark acceptance
    fi

    # 6a. A failing plan-time E2E smoke runs once more, in a call of its own,
    # so the check after a fix sees the machine as it is now (D46).
    smoke_record=$steps_dir/smoke
    if [ -f "$smoke_record" ] && ! dry_done smoke; then
        smoke_result=$(sed -n 's/^result: //p' "$smoke_record" | head -n 1)
        smoke_test=$(sed -n 's/^test: //p' "$smoke_record" | head -n 1)
        case $smoke_result in
            fail* | timeout)
                [ "$ran_here" -eq 0 ] || dry_more
                if path_is_safe "$smoke_test" && run_capture_e2e "$smoke_test" ".whygraph/plans/$stem/e2e/smoke"; then
                    ran_here=1
                    last=$(LC_ALL=C awk 'NF { l = $0 } END { print l }' <"$work/output")
                    case $code in
                        0) smoke_result=pass ;;
                        2) smoke_result="fail: $last" ;;
                        5) smoke_result=timeout ;;
                        *) smoke_result="not available: $last" ;;
                    esac
                    start_write "$smoke_record"
                    {
                        printf 'test: %s\n' "$smoke_test"
                        printf 'exit: %s\n' "$code"
                        printf 'result: %s\n' "$smoke_result"
                        printf '%s\n' '--- last 40 lines of output'
                        tail -n 40 "$work/output"
                    } >"$tmp"
                    finish_write "$smoke_record"
                fi
                ;;
        esac
        printf 'smoke: %s %s\n' "$smoke_test" "$smoke_result" >>"$record"
        dry_mark smoke
    fi

    # 7. The tree test, then the block.
    dry_finish
}

cmd_proof() {
    [ $# -eq 1 ] || usage
    plan=$1
    [ -f "$plan" ] || usage "no such plan: $plan"
    root=$(run_git rev-parse --show-toplevel) || usage "not in a git repository"
    work=$(mktemp -d) || exit 1
    trap 'rm -rf "$work"' EXIT
    # The plan path is relative to the caller, so read it before the cd.
    cat <"$plan" >"$work/plan" || usage "cannot read $plan"
    scan=$(plan_field "$work/plan" "" "")
    stem=$(stem_of "$plan")
    cd "$root" || exit 1
    check_work_dir
    proven=0
    unproven=0
    no_test=0
    other=0
    items=0
    literal=0
    passed=0
    empty=0
    while read -r kind number; do
        case $kind in
            step) ;;
            item)
                items=$((items + 1))
                [ -z "$(plan_field "$work/plan" "$number" Check)" ] || literal=$((literal + 1))
                record=$steps_dir/acceptance.$number
                if [ "$(record_exit "$record")" = 0 ]; then
                    if ran_no_test "$record"; then
                        empty=$((empty + 1))
                    else
                        passed=$((passed + 1))
                    fi
                fi
                continue
                ;;
            *) continue ;;
        esac
        base=
        [ ! -f "$steps_dir/baseline" ] || base=$(LC_ALL=C awk -v n="$number" '
            $1 == "step" && $2 == n { r = substr($0, index($0, ": ") + 2) }
            END { print r }
        ' <"$steps_dir/baseline")
        record=$steps_dir/$number.verify
        code=$(record_exit "$record")
        browser_red=
        grep -qx 'kind: browser' "$steps_dir/$number.red" 2>/dev/null && browser_red=1
        if [ "$code" = 0 ] && ran_no_test "$record"; then
            class='no test (the Verify ran no test)'
        elif [ "$code" = 0 ] && [ -n "$browser_red" ]; then
            red_result=$(sed -n 's/^result: //p' "$steps_dir/$number.red" | head -n 1)
            if ! test_first_intact "$work/plan" "$number" 2>/dev/null; then
                class='edited (the browser test was edited)'
            else
                case $red_result in
                    red) class='proven (browser test, red)' ;;
                    '') class='not proven (browser test, no red record)' ;;
                    *) class="not proven (browser test, $red_result)" ;;
                esac
            fi
        elif [ "$code" = 0 ] && [ "$base" = 'test first' ]; then
            # The red record decides. The guard runs first: end writes
            # exit 0 to <N>.verify before it exits 5.
            red_result=$(sed -n 's/^result: //p' "$steps_dir/$number.red" 2>/dev/null | head -n 1)
            if ! test_first_intact "$work/plan" "$number" 2>/dev/null; then
                class='edited (the Test first test was edited)'
            else
                case $red_result in
                    red) class='proven (test first, red)' ;;
                    'red (unnamed)') class='proven (test first, red unnamed)' ;;
                    'red (did not run)') class='proven (test first, red did not run, then passed)' ;;
                    passed) class='not proven (test first, passed before the change)' ;;
                    timeout) class='not proven (test first, red timed out)' ;;
                    'cannot run') class='not proven (test first, red cannot run)' ;;
                    'not run: '*) class="not proven (test first, red not run: ${red_result#not run: })" ;;
                    *) class='not proven (test first, no red record)' ;;
                esac
            fi
        elif [ "$code" = 0 ] && [ "${base#fail }" != "$base" ] &&
            waits_for_earlier "$work/plan" "$number"; then
            class='carried (failed before an earlier step)'
        elif [ "$code" = 0 ] && [ "${base#fail }" != "$base" ]; then
            class='proven (failed before the change)'
        elif [ "$code" = 0 ] && [ "$base" = pass ]; then
            class='unproven (passed before the change)'
        elif [ "$code" = 0 ] && [ "$base" = 'pass (no test ran)' ]; then
            class='no test (the Verify ran no test)'
        elif [ "$base" = 'not literal' ] || [ "$code" = 'not literal' ]; then
            class='no test (the Verify is not literal)'
        elif [ "$code" = missing ]; then
            class='not run'
        elif [ "$code" != 0 ]; then
            class=failed
        else
            class='unknown (no baseline in this run)'
        fi
        case $class in
            proven*) proven=$((proven + 1)) ;;
            unproven*) unproven=$((unproven + 1)) ;;
            'no test'*) no_test=$((no_test + 1)) ;;
            *) other=$((other + 1)) ;;
        esac
        printf 'proof: step %s %s\n' "$number" "$class"
        [ ! -f "$steps_dir/$number.diff" ] ||
            STEP=$number LC_ALL=C awk "$AWK_TEST_CHANGES" <"$steps_dir/$number.diff" >>"$work/tests"
    done <<EOF
$scan
EOF
    [ ! -s "$work/tests" ] || cat "$work/tests"
    steps=$((proven + unproven + no_test + other))
    printf 'proof: %s of %s steps proven, %s unproven, %s no test, %s other\n' \
        "$proven" "$steps" "$unproven" "$no_test" "$other"
    if [ "$items" -eq 0 ]; then
        printf 'proof: no Acceptance items\n'
    else
        printf 'proof: %s of %s Acceptance checks are literal, %s passed, %s ran no test\n' \
            "$literal" "$items" "$passed" "$empty"
    fi
    exit 0
}

# The directory of this script, before any cd: the capture script and the
# trust root are found from it.
tool_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd -P) || tool_dir=

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
    proof) cmd_proof "$@" ;;
    setup) cmd_setup "$@" ;;
    format) cmd_format "$@" ;;
    ready) cmd_ready "$@" ;;
    dryrun) cmd_dryrun "$@" ;;
    red) cmd_red "$@" ;;
    smoke) cmd_smoke "$@" ;;
    *) usage "unknown subcommand: $subcommand" ;;
esac
