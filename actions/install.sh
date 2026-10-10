# Install diffcone into a virtual environment of its own and put it on the
# PATH: the "Install diffcone" step of every action runs this.
#
# The runner has this action's repository at the action's ref, with
# GITHUB_ACTION_PATH the calling action's directory (actions/<name>), so by
# default diffcone matches the action; REF installs from that git ref
# instead. ``python3`` is ``python`` on Windows runners, and a venv keeps its
# programs in Scripts there. DIFFCONE_PYTHON names the environment's
# interpreter for later steps that import diffcone.
set -eo pipefail
py=$(command -v python3 || command -v python)
"$py" -m venv "$RUNNER_TEMP/diffcone"
bin="$RUNNER_TEMP/diffcone/bin"
[ -d "$bin" ] || bin="$RUNNER_TEMP/diffcone/Scripts"
if [ -n "${REF:-}" ]; then
  source="git+https://github.com/bearing-research/diffcone@$REF"
else
  source="$GITHUB_ACTION_PATH/../.."
fi
"$bin/python" -m pip install --quiet "$source"
echo "$bin" >> "$GITHUB_PATH"
echo "DIFFCONE_PYTHON=$bin/python" >> "$GITHUB_ENV"
