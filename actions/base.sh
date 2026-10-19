# Sourced by the run action: the base revision a run plans its change from.
#
# event_base sets ``base`` to the commit the event's change sits on, read
# from the event's own payload ($GITHUB_EVENT_PATH), or to "" when the event
# names none, and ``base_note`` to what it was taken from (or why there is
# none):
#
# - pull_request, pull_request_target: a checkout of the pull request's
#   merge commit (its second parent is the pull request's head) plans from
#   that merge commit's first parent, the target branch it was merged onto;
#   any other checkout (the pull request's head, pull_request_target's
#   default of the target branch) from pull_request.base.sha;
# - push: before, the commit the branch pointed at before the push, so a
#   push of several commits plans them all (all zeros when the push
#   created the branch: none);
# - merge_group: merge_group.base_sha, the target branch the queued pull
#   requests go onto;
# - any other event (workflow_dispatch, schedule): none.
#
# A shallow checkout's parents are read from the commit itself (git
# cat-file), which a depth-1 clone has even without the parents.
event_base() {
  base=""
  base_note=""
  local py found kind first second parents
  py=$(command -v python3 || command -v python)
  found=$("$py" - <<'PY'
import json, os
event = os.environ.get("GITHUB_EVENT_NAME", "")
try:
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
        payload = json.load(f)
except (KeyError, OSError, ValueError):
    payload = {}
if not isinstance(payload, dict):
    payload = {}
def get(*keys):
    value = payload
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) else ""
if event in ("pull_request", "pull_request_target"):
    print("pr", get("pull_request", "base", "sha") or "-", get("pull_request", "head", "sha") or "-")
elif event == "push":
    print("push", get("before") or "-")
elif event == "merge_group":
    print("merge_group", get("merge_group", "base_sha") or "-")
else:
    print("none", event or "-")
PY
  )
  found=${found%$'\r'}
  read -r kind first second <<<"$found"
  case "$kind" in
    pr)
      parents=$(git cat-file -p HEAD | sed -n 's/^parent //p' | tr -d '\r' | tr '\n' ' ')
      read -r -a parents <<<"$parents"
      if [ ${#parents[@]} -eq 2 ] && [ "${parents[1]}" = "$second" ]; then
        base=${parents[0]}
        base_note="the first parent of the pull request's merge commit"
      elif [ "$first" != "-" ]; then
        base=$first
        base_note="the pull request's base commit"
      else
        base_note="the pull request event names no base commit"
      fi
      ;;
    push)
      if [ "$first" = "-" ] || [[ "$first" =~ ^0+$ ]]; then
        base_note="the push created the branch: there is no earlier commit to compare with"
      else
        base=$first
        base_note="the commit the branch pointed at before the push"
      fi
      ;;
    merge_group)
      if [ "$first" = "-" ]; then
        base_note="the merge group event names no base commit"
      else
        base=$first
        base_note="the merge group's base commit"
      fi
      ;;
    *)
      base_note="a ${first:-} event names no base commit"
      ;;
  esac
}

# resolve_base makes the revision ``$BASE`` resolve in a shallow checkout
# (actions/checkout's default depth is 1) and sets ``base_sha`` to its
# commit; it returns 1 when the revision cannot be had. A revision relative
# to HEAD (HEAD^1, HEAD~3) deepens HEAD's history until it resolves, with
# --depth, not --deepen: GitHub deepens only from a commit a ref still
# points at, and a pull request's merge commit is replaced when the pull
# request moves (a re-run checks out its first attempt's). Anything else (a
# commit, a branch or tag name, origin/<branch>) is fetched itself; a name
# fetched lands in FETCH_HEAD only, so the commit is what the plan gets.
resolve_base() {
  base_sha=$(git rev-parse --verify --quiet "$BASE^{commit}" 2>/dev/null | tr -d '\r') || true
  [ -z "$base_sha" ] || return 0
  local head depth shallow
  head=$(git rev-parse HEAD | tr -d '\r')
  # A complete clone stays complete: --depth there would cut its history.
  shallow=$(git rev-parse --is-shallow-repository | tr -d '\r')
  case "$BASE" in
    HEAD|HEAD[~^]*)
      depth=$(( $(git rev-list --count HEAD | tr -d '\r') + 1 ))
      while [ "$shallow" = true ] && [ "$depth" -le 65536 ]; do
        git fetch --quiet --depth="$depth" origin "$head" 2>/dev/null || break
        base_sha=$(git rev-parse --verify --quiet "$BASE^{commit}" 2>/dev/null | tr -d '\r') || true
        [ -z "$base_sha" ] || return 0
        # The whole history is there and it still does not resolve.
        [ "$(git rev-parse --is-shallow-repository | tr -d '\r')" = true ] || break
        depth=$(( depth * 2 ))
      done
      ;;
    *)
      # A branch the checkout already has as origin/<branch>.
      base_sha=$(git rev-parse --verify --quiet "refs/remotes/origin/${BASE#origin/}^{commit}" \
        2>/dev/null | tr -d '\r') || true
      [ -z "$base_sha" ] || return 0
      local depth_arg=()
      [ "$shallow" != true ] || depth_arg=(--depth=1)
      if git fetch --quiet "${depth_arg[@]}" origin "${BASE#origin/}" 2>/dev/null; then
        base_sha=$(git rev-parse --verify --quiet "FETCH_HEAD^{commit}" 2>/dev/null | tr -d '\r') \
          || true
        [ -z "$base_sha" ] || return 0
      fi
      ;;
  esac
  base_sha=""
  return 1
}

# empty_base prints a commit of the empty tree, made locally: planned from
# it, every target is new, so every target is selected.
empty_base() {
  local tree
  tree=$(git mktree </dev/null | tr -d '\r')
  GIT_AUTHOR_NAME=diffcone GIT_AUTHOR_EMAIL=diffcone@invalid \
    GIT_COMMITTER_NAME=diffcone GIT_COMMITTER_EMAIL=diffcone@invalid \
    GIT_AUTHOR_DATE="1970-01-01T00:00:00Z" GIT_COMMITTER_DATE="1970-01-01T00:00:00Z" \
    git commit-tree "$tree" -m "diffcone: no base" | tr -d '\r'
}
