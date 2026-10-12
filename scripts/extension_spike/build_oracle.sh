#!/bin/bash
# Step 5a: a separate clone of X built with Cython profile/linetrace
# (sys.monitoring) and CYTHON_TRACE(_NOGIL), otherwise fixenv.sh's recipe.
# meson refuses add_project_arguments after the first target, so the
# arguments go right after the existing CYTHON_USE_TYPE_SPECS line.
set -euo pipefail
source "$(dirname "$0")/common.sh"
start=$(date +%s)
clone_at_x "$O"
cd "$O"
if ! grep -q "linetrace=True" meson.build; then
  python3 - <<'PY'
p = "meson.build"
s = open(p).read()
anchor = "add_project_arguments('-DCYTHON_USE_TYPE_SPECS=1', language: 'c')\n"
add = """
# ext-spike oracle: Cython profiling and line tracing (sys.monitoring)
add_project_arguments('-X', 'profile=True', '-X', 'linetrace=True', language: 'cython')
add_project_arguments('-DCYTHON_TRACE=1', '-DCYTHON_TRACE_NOGIL=1', language: 'c')
add_project_arguments('-DCYTHON_TRACE=1', '-DCYTHON_TRACE_NOGIL=1', language: 'cpp')
"""
assert anchor in s, "meson.build moved: find a new anchor"
open(p, "w").write(s.replace(anchor, anchor + add, 1))
PY
fi
# versioneer must see a clean checkout
git update-index --assume-unchanged meson.build
[[ -d .venv ]] || uv venv -q -p 3.13 .venv
export VIRTUAL_ENV=$O/.venv
export PATH=$O/.venv/bin:$PATH   # meson regenerates with cython from PATH
uv pip install -q -p .venv/bin/python -r requirements-dev.txt
rm -rf build
uv pip install -q -p .venv/bin/python --no-build-isolation --reinstall-package pandas -e .
.venv/bin/python -c "import pandas; print(pandas.__version__)"   # builds everything
echo "build done in $(( $(date +%s) - start )) s"
du -sh build .venv
