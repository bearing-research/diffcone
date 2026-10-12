# Shared settings for the item 18 spike drivers (sourced, not run).
#
# SP is the spike's data directory: clones, venvs, builds, the store and logs.
# It defaults to the worktree's untracked .spike/ (on the persistent disk).
W=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SP=${SP:-$W/.spike}
X=${X:-bc2fe1b0a0e0d28b0b027e8cc7b8abca5c7faff1}
TAG=${TAG:-v3.2.0.dev0}     # versioneer needs a reachable vX.Y.0.dev0 tag
E=$SP/repos/pandas          # ordinary build (fixenv.sh's pandas recipe)
O=$SP/pandas-oracle         # profiled, line-traced build of X
MARKERS="not slow and not network and not db and not single_cpu"
mkdir -p "$SP/logs" "$SP/data" "$SP/cache"

# A clone of pandas at X with the 500 first-parent commits before it and the
# dev0 tag, without the ~70k-commit full history.
clone_at_x() {
  local dir=$1
  if [[ ! -d $dir/.git ]]; then
    git clone --quiet --no-tags --single-branch --depth 1 https://github.com/pandas-dev/pandas "$dir"
  fi
  git -C "$dir" fetch --quiet --depth 700 origin "$X"
  git -C "$dir" fetch --quiet --depth 700 origin tag "$TAG"
  git -C "$dir" checkout --quiet --detach "$X"
  echo "$dir at $(git -C "$dir" describe --tags), $(git -C "$dir" rev-list --first-parent --count HEAD) first-parent commits"
}
