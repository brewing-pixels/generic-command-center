#!/usr/bin/env bash
# Refuse to record things that must stay local or secret.
#
#   secret-scan.sh --tree            scan every tracked file (CI)
#   secret-scan.sh --staged          scan the lines a commit would add (pre-commit)
#   secret-scan.sh --message <file>  scan a commit message (commit-msg)
#
# Rules:
#   1. local config and local-only assets are never tracked
#   2. the operator PIN from config.yaml never appears in a file or a message
#   3. key-shaped strings (sk-, ghp_, AKIA, private key headers) are refused
#
# False positives: append `allowlist-secret` to the offending line.
set -uo pipefail

root=$(git rev-parse --show-toplevel) || exit 1
cd "$root" || exit 1

mode=${1:---tree}
status=0
skip=(':!uv.lock' ':!*.lock')

report() {
    printf 'secret-scan: %s\n' "$*" >&2
    status=1
}

# Every added line in the index, as "path: content".
added_lines() {
    git diff --cached -U0 -- . "${skip[@]}" | awk '
        /^\+\+\+ b\// { file = substr($0, 7); next }
        /^\+/ { print file ": " substr($0, 2) }
    '
}

# scan <fixed|extended> <pattern> — prints offending "path: content" lines.
scan() {
    case "$mode" in
        --staged)
            if [ "$1" = fixed ]; then
                added_lines | grep -F -- "$2" || true
            else
                added_lines | grep -E -- "$2" || true
            fi
            ;;
        *)
            if [ "$1" = fixed ]; then
                git grep -nIF -- "$2" -- . "${skip[@]}" 2>/dev/null || true
            else
                git grep -nIE -- "$2" -- . "${skip[@]}" 2>/dev/null || true
            fi
            ;;
    esac
}

# ---------------------------------------------------------------- 1. paths
case "$mode" in
    --message) files="" ;;
    --staged) files=$(git diff --cached --name-only --diff-filter=ACM) ;;
    *) files=$(git ls-files) ;;
esac

if [ -n "$files" ]; then
    bad=$(printf '%s\n' "$files" \
        | grep -E '(^|/)config\.yaml$|(^|/)config\.[^/]*\.yaml$|\.local\.[^/]*$' \
        | grep -v '^config\.example\.yaml$' || true)
    while IFS= read -r path; do
        [ -n "$path" ] && report "must not be tracked: $path — keep local files ignored, and name the template config.example.yaml"
    done <<< "$bad"
fi

# ----------------------------------------------------------------- 2. the PIN
# Read the live PIN from whichever config the daemon would load. Absent in CI,
# so a clone never trips on a stranger's value.
pin=""
for candidate in "${CC_CONFIG:-}" config.yaml "$HOME/.config/command-center/config.yaml"; do
    [ -n "$candidate" ] && [ -f "$candidate" ] || continue
    pin=$(grep -E '^[[:space:]]*pin:' "$candidate" | head -n1 \
        | sed -E 's/.*pin:[[:space:]]*//; s/[[:space:]]*#.*//' \
        | tr -d "\"'[:space:]")
    [ -n "$pin" ] && break
done

if [ -n "$pin" ]; then
    if [ "$mode" = "--message" ]; then
        msg_file=${2:-}
        if [ -n "$msg_file" ] && [ -f "$msg_file" ] && grep -qF -- "$pin" "$msg_file"; then
            report "the operator PIN appears in the commit message — describe the mechanism, not the value"
        fi
    else
        while IFS= read -r hit; do
            [ -n "$hit" ] && report "the operator PIN appears in $hit — use a random value (2468), or add allowlist-secret"
        done <<< "$(scan fixed "$pin" | grep -v 'allowlist-secret' || true)"
    fi
fi

# ------------------------------------------------------- 3. key-shaped strings
keys='sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

if [ "$mode" = "--message" ]; then
    msg_file=${2:-}
    if [ -n "$msg_file" ] && [ -f "$msg_file" ] && grep -qE -- "$keys" "$msg_file"; then
        report "a key-shaped string appears in the commit message"
    fi
else
    while IFS= read -r hit; do
        [ -n "$hit" ] && report "possible credential: $hit — add allowlist-secret if it is a fixture"
    done <<< "$(scan extended "$keys" | grep -v 'allowlist-secret' || true)"
fi

# ------------------------------------------------- 4. a PIN that is not a placeholder
# Rule 2 needs a readable config, so it is blind in CI and in a fresh clone.
# This one needs no secret: no tracked file outside tests/ may carry a real
# 4-8 digit PIN, only the documented 0000 placeholder.
pin_rule="pin[\"']?[[:space:]]*:[[:space:]]*[\"']?[0-9]{4,8}"
pin_placeholder="pin[\"']?[[:space:]]*:[[:space:]]*[\"']?0000[\"']?[[:space:]]*(#|\$)"

if [ "$mode" != "--message" ]; then
    hits=$(scan extended "$pin_rule" \
        | grep -v 'allowlist-secret' \
        | grep -vE -- "$pin_placeholder" \
        | grep -vE '^\.?/?tests/' || true)
    while IFS= read -r hit; do
        [ -n "$hit" ] && report "a real PIN in $hit — only the 0000 placeholder belongs in a tracked file (tests/ excepted)"
    done <<< "$hits"
fi

if [ "$status" -ne 0 ]; then
    printf 'secret-scan: refused\n' >&2
fi
exit "$status"
