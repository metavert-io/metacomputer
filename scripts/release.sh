#!/bin/bash
# release.sh: make a release of metacomputer in two steps, each through a pull request or a tag, never a push to
# main. Run it from a checkout of this repository (any folder inside it).
#
#   scripts/release.sh VERSION [--dry-run] [--skip-tests]
#       1. Prepare. Refuses unless: you're on main, up to date with origin/main, with a clean tree; the CHANGELOG
#          has an [Unreleased] section with entries; scripts/check-public.sh -a passes; and every no-VM test suite
#          passes (NO_VM_SUITES below, the list in CLAUDE.md). Then it moves [Unreleased] to [VERSION] - DATE
#          (a new, empty [Unreleased] above it), commits that on a branch release-vVERSION, pushes the branch,
#          opens a pull request against main (gh) and switches back to main. Merging the PR is up to you.
#
#   scripts/release.sh VERSION --tag [--draft-release] [--dry-run]
#       2. After the PR is merged: tags its merge commit vVERSION (annotated), pushes the tag, and runs get.sh's
#          dry run (the get.sh on origin/main, which the one-line install downloads) in a temporary HOME to
#          confirm it now installs vVERSION. --draft-release also drafts a GitHub Release from the CHANGELOG
#          section: a draft only, never published (publishing it is the owner's decision).
#
#   --dry-run      run every check and show what it would do (the CHANGELOG change as a diff, each command);
#                  change nothing (it does fetch origin, to know whether main is up to date)
#   --skip-tests   with --dry-run only: leave out the test suites (a quick look at the rest); a real run always
#                  runs them
#
# VERSION is MAJOR.MINOR.PATCH (get.sh installs the newest vX.Y.Z tag) and must be newer than every v* tag.
# Nothing here asks for or handles a credential: pushing and gh use your own git and gh setup.
set -uo pipefail

NO_VM_SUITES=(test_actionlog test_recording test_vm_shell test_vm_claude test_ssh_options test_hub test_secrets test_vault_ops
  test_vault_tokens test_vault_approvals test_vault_import test_secret_fill test_guest_import test_termsafe test_key_pinning
  test_build_refusals test_owners test_bench_workdir test_bench_checks test_vm_runner test_vm_stop test_reset_sync
  test_plugin test_learn test_storage test_install test_events test_doc_claims test_release test_handoff
  test_hidden_text test_multi_vm test_vault_codes test_guest_profiles test_netpolicy test_botcheck
  test_traces test_page_guest test_update test_agents test_cloud_adapters test_session_guard test_remote)
REMOTE=origin
MAIN=main
CANONICAL_SLUG="metavert-io/metacomputer"   # get.sh's own repository: its dry run needs no --repo for it

usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
if [ -t 1 ]; then b=$'\033[1m'; g=$'\033[32m'; y=$'\033[33m'; r=$'\033[31m'; o=$'\033[0m'; else b=; g=; y=; r=; o=; fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s\n' "  ${g}✓${o} $*"; }
warn() { printf '%s\n' "  ${y}!${o} $*"; }
step() { printf '\n%s\n' "${b}$*${o}"; }
die()  { printf '\n%s\n' "${r}release: $1${o}" >&2; shift; local l; for l in "$@"; do printf '%s\n' "  $l" >&2; done; exit 1; }
would() { printf '%s\n' "  would run: $*"; }

VERSION="" TAG_STEP=0 DRY=0 SKIP_TESTS=0 DRAFT=0
for a in "$@"; do
  case "$a" in
    --tag) TAG_STEP=1 ;;
    --dry-run) DRY=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --draft-release) DRAFT=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "release: unknown option $a (see --help)" >&2; exit 2 ;;
    *) [ -z "$VERSION" ] || { echo "release: one VERSION only (see --help)" >&2; exit 2; }; VERSION="$a" ;;
  esac
done
[ -n "$VERSION" ] || { usage >&2; exit 2; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "release: VERSION is MAJOR.MINOR.PATCH, like 0.3.0 (no v)" >&2; exit 2; }
[ "$SKIP_TESTS" = 0 ] || [ "$DRY" = 1 ] || { echo "release: --skip-tests goes with --dry-run only" >&2; exit 2; }
[ "$DRAFT" = 0 ] || [ "$TAG_STEP" = 1 ] || { echo "release: --draft-release goes with --tag" >&2; exit 2; }

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null)" || die "not in a git checkout"
cd "$ROOT" || die "can't enter $ROOT"
TAG="v$VERSION" BRANCH="release-v$VERSION"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-release.XXXXXX")" || die "couldn't make a temporary folder"
trap '"$ROOT/scripts/safe-rmtree.sh" --quiet "$TMP"' EXIT
command -v python3 >/dev/null 2>&1 || die "python3 not found"

# --- shared checks ------------------------------------------------------------------------------------------------
newest_tag() { # REMOTE_TAG_NAMES: the newest vX.Y.Z tag, here or on the remote
  { git tag -l 'v*'; printf '%s\n' "$1"; } | python3 -c '
import re, sys
vs = {tuple(map(int, l.strip()[1:].split("."))) for l in sys.stdin if re.fullmatch(r"v\d+\.\d+\.\d+", l.strip())}
print("v" + ".".join(map(str, max(vs))) if vs else "")'
}
newer() { # A B: A is a newer MAJOR.MINOR.PATCH than B
  python3 -c 'import sys; a, b = (tuple(map(int, v.split("."))) for v in sys.argv[1:3]); sys.exit(0 if a > b else 1)' "$1" "$2"
}
tag_free() {
  git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "the tag $TAG already exists here"
  local remote_tags last
  remote_tags="$(git ls-remote --tags --refs "$REMOTE" 'v*')" || die "couldn't list $REMOTE's tags"
  remote_tags="$(printf '%s\n' "$remote_tags" | sed -n 's#.*refs/tags/##p')"
  ! printf '%s\n' "$remote_tags" | grep -qxF "$TAG" || die "the tag $TAG already exists on $REMOTE"
  last="$(newest_tag "$remote_tags")" || die "couldn't compare the release tags"
  if [ -n "$last" ]; then
    newer "$VERSION" "${last#v}" || die "$VERSION isn't newer than the newest release tag, $last"
  fi
  ok "$TAG is free and newer than ${last:-every tag (there are none)}"
}
check_public() { # standalone, gated on its exit code (never piped)
  say "  scripts/check-public.sh -a"
  if "$ROOT/scripts/check-public.sh" -a; then
    ok "check-public: nothing found"
  else
    die "scripts/check-public.sh -a found something (above), so nothing was released" \
      "Fix it, commit through a PR, and run this again."
  fi
}
section_of() { # FILE VERSION: the body of CHANGELOG's ## [VERSION] section
  python3 - "$1" "$2" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^## \[" + re.escape(sys.argv[2]) + r"\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
sys.stdout.write(m.group(1).strip() + "\n" if m else "")
PY
}

# --- step 2: tag the merged release ----------------------------------------------------------------------------
if [ "$TAG_STEP" = 1 ]; then
  step "Tagging $TAG$([ "$DRY" = 1 ] && echo "  (dry run: nothing changes)")"
  command -v gh >/dev/null 2>&1 || die "gh (GitHub's CLI) not found: it finds the merged pull request"
  git fetch -q "$REMOTE" "$MAIN" || die "couldn't fetch $REMOTE/$MAIN"
  tag_free
  prs="$(gh pr list --state all --head "$BRANCH" --base "$MAIN" --json number,state,mergeCommit,url)" \
    || die "gh couldn't list the pull requests for $BRANCH"
  merge="$(python3 -c '
import json, sys
prs = [p for p in json.loads(sys.argv[1] or "[]") if p.get("state") == "MERGED" and (p.get("mergeCommit") or {}).get("oid")]
print(prs[0]["mergeCommit"]["oid"] + " " + prs[0]["url"] if prs else "")' "$prs")" || die "couldn't read gh's answer"
  [ -n "$merge" ] || die "no merged pull request from $BRANCH into $MAIN yet" \
    "Prepare it with: scripts/release.sh $VERSION, and merge the pull request it opens; then run this again."
  sha="${merge%% *}" url="${merge#* }"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "gh named an odd merge commit: $sha"
  git merge-base --is-ancestor "$sha" "$REMOTE/$MAIN" 2>/dev/null \
    || die "the merge commit $sha (from $url) isn't on $REMOTE/$MAIN"
  ok "merged: $url, merge commit ${sha:0:12} on $REMOTE/$MAIN"
  git show "$sha:CHANGELOG.md" > "$TMP/CHANGELOG.md" 2>/dev/null || die "no CHANGELOG.md in $sha"
  grep -qE "^## \[$VERSION\] - [0-9]{4}-[0-9]{2}-[0-9]{2}$" "$TMP/CHANGELOG.md" \
    || die "the CHANGELOG at ${sha:0:12} has no '## [$VERSION] - DATE' section"
  section_of "$TMP/CHANGELOG.md" "$VERSION" > "$TMP/notes.md"
  ok "the CHANGELOG at ${sha:0:12} has [$VERSION]"
  check_public

  url_origin="$(git remote get-url "$REMOTE")" || die "no remote $REMOTE"
  slug="$(printf '%s' "$url_origin" | sed -E 's#^(https://|ssh://git@|git@)github\.com[:/]##; s#\.git$##; s#/$##')"
  repo_arg=(); [ "$slug" = "$CANONICAL_SLUG" ] || repo_arg=(--repo "$url_origin")   # empty for the real repository (bash 3.2: guard it)
  git show "$REMOTE/$MAIN:get.sh" > "$TMP/get.sh" 2>/dev/null || die "no get.sh on $REMOTE/$MAIN"
  getsh_check() { # the release get.sh's dry run picks, from a temporary HOME
    mkdir -p "$TMP/home"
    HOME="$TMP/home" bash "$TMP/get.sh" --dry-run ${repo_arg[@]+"${repo_arg[@]}"} < /dev/null > "$TMP/get.log" 2>&1
  }

  if [ "$DRY" = 1 ]; then
    would git tag -a "$TAG" "${sha:0:12}" -m "\"metacomputer $VERSION\""
    would git push "$REMOTE" "refs/tags/$TAG"
    if getsh_check; then
      now="$(grep -o 'would check out [^,]*' "$TMP/get.log" | head -1)"
      say "  get.sh's dry run today: ${now:-no release line (see below)}; after the push it must say: would check out release $TAG"
    else
      warn "get.sh's dry run failed today (exit status above 0); its output:"; cat "$TMP/get.log"
    fi
    [ "$DRAFT" = 0 ] || would gh release create "$TAG" --draft --verify-tag --title "\"metacomputer $VERSION\"" --notes-file NOTES
    say ""; say "Dry run: nothing was changed. The release notes would be:"; cat "$TMP/notes.md"
    exit 0
  fi

  git tag -a "$TAG" "$sha" -m "metacomputer $VERSION" || die "couldn't make the tag $TAG"
  ok "tagged ${sha:0:12} as $TAG (annotated)"
  git push "$REMOTE" "refs/tags/$TAG" || die "couldn't push $TAG to $REMOTE (the tag is here; push it with: git push $REMOTE refs/tags/$TAG)"
  ok "pushed $TAG to $REMOTE"
  step "Checking that the one-line install picks $TAG (get.sh --dry-run, temporary HOME)"
  if ! getsh_check; then
    cat "$TMP/get.log"
    die "get.sh's dry run failed (its output is above); the tag $TAG is pushed" "Look into it before announcing the release."
  fi
  if grep -q "would check out release $TAG, commit ${sha:0:12}" "$TMP/get.log"; then
    ok "get.sh would install $TAG (commit ${sha:0:12})"
  else
    cat "$TMP/get.log"
    die "get.sh's dry run doesn't pick $TAG at ${sha:0:12} (its output is above); the tag is pushed" \
      "Look into it before announcing the release."
  fi
  if [ "$DRAFT" = 1 ]; then
    rel="$(gh release create "$TAG" --draft --verify-tag --title "metacomputer $VERSION" --notes-file "$TMP/notes.md")" \
      || die "gh couldn't draft the release (the tag is pushed): gh release create $TAG --draft --notes-file NOTES"
    ok "drafted a GitHub Release (a draft, not published): $rel"
    say "  Publishing it is your decision: review it on GitHub, then publish it there."
  fi
  say ""; say "${g}Released $TAG.${o}"
  exit 0
fi

# --- step 1: prepare the release PR --------------------------------------------------------------------------------
step "Preparing $VERSION$([ "$DRY" = 1 ] && echo "  (dry run: nothing changes)")"
head_branch="$(git symbolic-ref -q --short HEAD)" || die "not on a branch (a detached HEAD): switch to $MAIN"
[ "$head_branch" = "$MAIN" ] || die "on the branch $head_branch, not $MAIN: git switch $MAIN"
dirty="$(git status --porcelain --untracked-files=normal)"
[ -z "$dirty" ] || die "the tree isn't clean (commit, move or remove these first):" "$dirty"
git fetch -q "$REMOTE" "$MAIN" || die "couldn't fetch $REMOTE/$MAIN"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "$REMOTE/$MAIN")" ] \
  || die "$MAIN isn't $REMOTE/$MAIN (ahead, behind or both): git pull --ff-only, or look at what differs"
ok "on $MAIN at $(git rev-parse --short=12 HEAD), the same as $REMOTE/$MAIN, clean tree"
tag_free
git rev-parse -q --verify "refs/heads/$BRANCH" >/dev/null && die "the branch $BRANCH already exists here"
remote_branch="$(git ls-remote --heads "$REMOTE" "refs/heads/$BRANCH")" || die "couldn't list $REMOTE's branches"
[ -z "$remote_branch" ] || die "the branch $BRANCH already exists on $REMOTE"

unreleased_entries() { # FILE VERSION: how many entries [Unreleased] has (or HAS_VERSION / NO_SECTION)
  python3 - "$1" "$2" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
if re.search(r"^## \[" + re.escape(sys.argv[2]) + r"\]", text, re.M):
    print("HAS_VERSION")
    sys.exit()
m = re.search(r"^## \[Unreleased\][ \t]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
print(sum(1 for line in m.group(1).splitlines() if line.startswith("- ")) if m else "NO_SECTION")
PY
}
entries="$(unreleased_entries CHANGELOG.md "$VERSION")"
case "$entries" in
  HAS_VERSION) die "the CHANGELOG already has a [$VERSION] section" ;;
  NO_SECTION) die "the CHANGELOG has no '## [Unreleased]' section" ;;
  0|"") die "the CHANGELOG's [Unreleased] section has no entries ('- ' lines): nothing to release" ;;
esac
ok "the CHANGELOG's [Unreleased] has $entries entries"
check_public

if [ "$SKIP_TESTS" = 1 ]; then
  warn "test suites left out (--skip-tests, dry run only)"
else
  step "No-VM test suites (${#NO_VM_SUITES[@]}; logs in $TMP/tests until this ends)"
  mkdir -p "$TMP/tests"; failed=()
  for s in ${NO_VM_SUITES[@]+"${NO_VM_SUITES[@]}"}; do
    t0=$SECONDS
    if [ ! -f "tests/$s.py" ]; then failed+=("$s (missing)"); printf '  %s %s: missing\n' "${r}✗${o}" "$s"; continue; fi
    # standalone, each gated on its own exit status; its output goes to a file, not through a pipe. Session records
    # (lib/sessionvm.py) of the bridges and claims a suite makes go to a folder of the run's own, never the account's.
    if env -u MCOMP_CONFIG -u CLAUDECODE MCOMP_SESSIONS_DIR="$TMP/sessions" python3 "tests/$s.py" > "$TMP/tests/$s.log" 2>&1 < /dev/null; then
      printf '  %s %s (%ss)\n' "${g}✓${o}" "$s" "$((SECONDS - t0))"
    else
      printf '  %s %s (%ss): failed\n' "${r}✗${o}" "$s" "$((SECONDS - t0))"
      failed+=("$s")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then
    keep="$(mktemp -d "${TMPDIR:-/tmp}/mcomp-release-logs.XXXXXX")" && cp "$TMP"/tests/*.log "$keep/" 2>/dev/null
    die "${#failed[@]} suite(s) failed: ${failed[*]}" "Their logs: ${keep:-(not kept)}"
  fi
  ok "all ${#NO_VM_SUITES[@]} suites passed"
fi

DATE="$(date +%Y-%m-%d)"
cp CHANGELOG.md "$TMP/CHANGELOG.new"
python3 - "$TMP/CHANGELOG.new" "$VERSION" "$DATE" <<'PY' || die "couldn't rewrite the CHANGELOG"
import re, sys
path, version, date = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
new, n = re.subn(r"^## \[Unreleased\][ \t]*\n", f"## [Unreleased]\n\n## [{version}] - {date}\n", text, count=1, flags=re.M)
if n != 1:
    sys.exit(1)
new = re.sub(r"(## \[" + re.escape(version) + r"\] - [0-9-]+\n)\n*", r"\1\n", new, count=1)
open(path, "w", encoding="utf-8").write(new)
PY
section_of "$TMP/CHANGELOG.new" "$VERSION" > "$TMP/notes.md"
{
  printf 'Release %s: moves the CHANGELOG'"'"'s [Unreleased] entries to [%s] - %s.\n\n' "$VERSION" "$VERSION" "$DATE"
  printf 'Checked before this PR was opened: main at %s, a clean tree, `scripts/check-public.sh -a`, and the %s no-VM suites%s.\n\n' \
    "$(git rev-parse --short=12 HEAD)" "${#NO_VM_SUITES[@]}" "$([ "$SKIP_TESTS" = 1 ] && echo " (left out: dry run)")"
  printf 'After merging: `scripts/release.sh %s --tag` tags the merge commit %s, pushes the tag and checks that get.sh installs it.\n\n' "$VERSION" "$TAG"
  printf '## %s\n\n' "$VERSION"; cat "$TMP/notes.md"
} > "$TMP/pr.md"

if [ "$DRY" = 1 ]; then
  step "What it would do"
  say "  the CHANGELOG change:"
  diff -u CHANGELOG.md "$TMP/CHANGELOG.new" | sed 's/^/    /'
  would git switch -c "$BRANCH"
  would git commit -m "\"Release $VERSION: CHANGELOG\"" CHANGELOG.md
  would scripts/check-public.sh -a
  would git push -u "$REMOTE" "HEAD:refs/heads/$BRANCH"
  would gh pr create --base "$MAIN" --head "$BRANCH" --title "\"Release $VERSION\"" --body-file PR.md
  would git switch "$MAIN"
  say ""; say "Dry run: nothing was changed."
  exit 0
fi

command -v gh >/dev/null 2>&1 || die "gh (GitHub's CLI) not found: it opens the pull request"
git switch -q -c "$BRANCH" || die "couldn't make the branch $BRANCH"
cp "$TMP/CHANGELOG.new" CHANGELOG.md
back() { git switch -q "$MAIN" 2>/dev/null || warn "couldn't switch back to $MAIN"; }
if ! git commit -q -m "Release $VERSION: CHANGELOG" -- CHANGELOG.md; then
  git checkout -q -- CHANGELOG.md; back; git branch -q -D "$BRANCH"
  die "couldn't commit the CHANGELOG (nothing was pushed)"
fi
ok "committed the CHANGELOG on $BRANCH ($(git rev-parse --short=12 HEAD))"
say "  scripts/check-public.sh -a, again, before the push"
if ! "$ROOT/scripts/check-public.sh" -a; then
  back
  die "check-public found something after the commit (above); nothing was pushed" "The commit stays on $BRANCH for you to look at."
fi
git push -q -u "$REMOTE" "HEAD:refs/heads/$BRANCH" || { back; die "couldn't push $BRANCH (it stays here)"; }
ok "pushed $BRANCH to $REMOTE"
pr="$(gh pr create --base "$MAIN" --head "$BRANCH" --title "Release $VERSION" --body-file "$TMP/pr.md")" \
  || { back; die "gh couldn't open the pull request; $BRANCH is pushed: gh pr create --base $MAIN --head $BRANCH"; }
back
ok "opened $pr"
say ""
say "${g}Next:${o} review and merge $pr; then: scripts/release.sh $VERSION --tag"
