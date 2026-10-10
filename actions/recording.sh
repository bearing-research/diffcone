# Sourced by the run and record actions: the evidence store diffcone record
# saved, as restored into .diffcone/evidence/ (record keeps only its own
# commit's, so there is at most one).
#
# find_recording sets ``store`` (its path, "" for none) and ``commit`` (the
# commit it was recorded at), and returns 0 when the store is there and its
# commit is in the checkout (fetched when a shallow checkout lacks it), 1
# when no store was restored, and 2 when its commit is not to be had (a
# force-pushed branch may have lost it).
find_recording() {
  store=$(ls .diffcone/evidence/*.sqlite 2>/dev/null | head -1 || true)
  commit=""
  [ -n "$store" ] || return 1
  commit=$(basename "$store" | cut -d- -f1)
  git cat-file -e "$commit^{commit}" 2>/dev/null \
    || git fetch --quiet --depth=1 origin "$commit" \
    || return 2
}
