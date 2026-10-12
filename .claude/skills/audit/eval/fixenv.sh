#!/usr/bin/env bash
# Recreate one evaluation repository's test environment from scratch.
#
#   fixenv.sh NAME            # clone if missing, rebuild NAME/.venv, check collection
#   fixenv.sh --list          # the names this script knows
#   FIXENV_KEEP_VENV=1 fixenv.sh NAME   # install into the existing .venv
#
# NAME is the repository's basename (flask, coveragepy, pandas, ...). Clones
# go to $E/repos/NAME (--depth 40, single branch, no tags) and each env is
# NAME/.venv, built with uv the way the project's own CI installs its tests
# (dependency groups, extras, requirements files, tox deps), plus pytest-cov.
# Recipes and their last measured status are in envs.md beside this script.
#
# E (required) is the evaluation work directory. Python is 3.13 unless the recipe
# says otherwise (PY=3.12 fixenv.sh NAME overrides).
set -euo pipefail

E=${E:?set E to the evaluation work directory (clones and logs go there)}
REPOS=$E/repos
LOGS=$E/logs
mkdir -p "$REPOS" "$LOGS"

slug_of() {
  case $1 in
    flask|jinja|werkzeug|itsdangerous) echo "pallets/$1" ;;
    httpx|starlette|uvicorn) echo "encode/$1" ;;
    requests|black) echo "psf/$1" ;;
    urllib3) echo "urllib3/urllib3" ;;
    rich) echo "Textualize/rich" ;;
    typer|fastapi) echo "fastapi/$1" ;;
    pydantic) echo "pydantic/pydantic" ;;
    marshmallow) echo "marshmallow-code/marshmallow" ;;
    cattrs) echo "python-attrs/cattrs" ;;
    tenacity) echo "jd/tenacity" ;;
    python-dotenv) echo "theskumar/python-dotenv" ;;
    pluggy|pytest-xdist|pytest-asyncio) echo "pytest-dev/$1" ;;
    pip|packaging|build|twine|virtualenv) echo "pypa/$1" ;;
    tox) echo "tox-dev/tox" ;;
    pre-commit) echo "pre-commit/pre-commit" ;;
    isort|flake8) echo "PyCQA/$1" ;;
    coveragepy) echo "nedbat/coveragepy" ;;
    arrow) echo "arrow-py/arrow" ;;
    dateutil) echo "dateutil/dateutil" ;;
    more-itertools) echo "more-itertools/more-itertools" ;;
    boltons) echo "mahmoud/boltons" ;;
    poetry) echo "python-poetry/poetry" ;;
    alembic) echo "sqlalchemy/alembic" ;;
    networkx) echo "networkx/networkx" ;;
    scrapy) echo "scrapy/scrapy" ;;
    anyio) echo "agronholm/anyio" ;;
    trio) echo "python-trio/trio" ;;
    pygments) echo "pygments/pygments" ;;
    pandas) echo "pandas-dev/pandas" ;;
    *) return 1 ;;
  esac
}

ALL="flask jinja werkzeug itsdangerous httpx starlette uvicorn requests urllib3
rich typer fastapi pydantic marshmallow cattrs tenacity python-dotenv pluggy
pytest-xdist pytest-asyncio pip packaging build twine virtualenv tox pre-commit
isort flake8 coveragepy arrow dateutil more-itertools boltons poetry black
alembic networkx scrapy anyio trio pygments pandas"

if [[ ${1:-} == --list ]]; then echo $ALL; exit 0; fi
name=${1:?usage: fixenv.sh NAME}
slug=$(slug_of "$name") || { echo "unknown repository: $name" >&2; exit 2; }
R=$REPOS/$name

if [[ ! -d $R/.git ]]; then
  git clone --quiet --depth 40 --no-tags --single-branch "https://github.com/$slug.git" "$R"
fi
cd "$R"

PY=${PY:-3.13}
if [[ -z ${FIXENV_KEEP_VENV:-} ]]; then
  rm -rf .venv
  uv venv -q -p "$PY" .venv
fi
export VIRTUAL_ENV=$R/.venv

# The clone has no tags, so setuptools-scm / hatch-vcs would version the
# project 0.1.devN, and resolvers then pick ancient releases of anything that
# requires it (pluggy under pytest, dateutil under freezegun). Pretend to be
# the latest release on PyPI; projects with a static version ignore this.
dist=$(python3 - <<'PY'
import configparser, os, tomllib
name = None
if os.path.exists("pyproject.toml"):
    name = tomllib.load(open("pyproject.toml", "rb")).get("project", {}).get("name")
if not name and os.path.exists("setup.cfg"):
    c = configparser.ConfigParser(); c.read("setup.cfg")
    name = c.get("metadata", "name", fallback=None)
print(name or "")
PY
)
if [[ -n $dist && $name != pandas ]]; then
  ver=$(curl -fsSL "https://pypi.org/pypi/$dist/json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' || true)
  if [[ -n $ver ]]; then export SETUPTOOLS_SCM_PRETEND_VERSION=$ver; fi
fi
pip_() { uv pip install -q -p .venv/bin/python "$@"; }

case $name in
  # pallets: dependency group "tests" (uv sync in CI)
  flask) pip_ -e '.[async,dotenv]' --group tests ;;
  jinja) pip_ -e '.[i18n]' --group tests ;;
  werkzeug) pip_ -e '.[watchdog]' --group tests ;;
  itsdangerous) pip_ -e . --group tests ;;
  # httpx: scripts/install = pip install -r requirements.txt (-e .[all extras])
  httpx) pip_ -r requirements.txt ;;
  # starlette, uvicorn: scripts/install = uv sync --frozen (default group dev)
  starlette) pip_ -e '.[full]' --group dev ;;
  uvicorn) pip_ -e '.[standard]' --group dev ;;
  requests) pip_ -e '.[socks]' --group test ;;
  # urllib3: noxfile test session = group dev + extras socks,brotli,zstd,h2
  urllib3) pip_ -e '.[socks,brotli,zstd,h2]' --group dev ;;
  # rich: poetry dev-dependencies (tox runs `poetry install`)
  rich) pip_ -e . pytest attrs typing-extensions ;;
  typer) pip_ -e . --group tests ;;
  fastapi) pip_ -e '.[all]' --group tests ;;
  # pydantic: uv workspace with pydantic-core (Rust, built from source by
  # maturin: needs cargo); CI: uv sync --all-packages --all-extras --group
  # testing-extra, and pydantic-core's own testing-extra for tests/pydantic_core
  pydantic) pip_ -e '.[email,timezone]' --group dev --group testing-extra --group pydantic-core/pyproject.toml:testing-extra ;;
  marshmallow) pip_ -e . --group tests ;;
  # cattrs: justfile = uv sync --all-groups --all-extras; tests run with test+lint
  cattrs) pip_ -e '.[ujson,orjson,msgpack,pyyaml,tomlkit,cbor2,bson,msgspec]' --group test --group lint ;;
  # tenacity: uv run poe <task> (default group dev)
  tenacity) pip_ -e '.[test]' --group dev ;;
  # python-dotenv: tox deps
  python-dotenv) pip_ -e '.[cli]' pytest click ipython ;;
  pluggy) pip_ -e . --group testing ;;
  # pytest-xdist: tox extras testing (+ psutil env)
  pytest-xdist) pip_ -e '.[testing,psutil,setproctitle]' pytest ;;
  pytest-asyncio) pip_ -e . --group test ;;
  # pip: noxfile test session = group test, plus the wheels its tests install
  # offline (tests/data/common_wheels, built from group test-common-wheels)
  pip)
    pip_ -e . --group test
    rm -rf tests/data/common_wheels
    .venv/bin/python -m pip wheel -q -w tests/data/common_wheels --group test-common-wheels ;;
  packaging) pip_ -e . --group test ;;
  build) pip_ -e '.[uv,virtualenv]' --group test ;;
  # twine: tox deps
  twine) pip_ -e '.[keyring]' pretend pytest pytest-socket coverage build ;;
  virtualenv) pip_ -e . --group test ;;
  # tox: tox.toml env = dependency group test + extra completion
  tox) pip_ -e '.[completion]' --group test ;;
  pre-commit) pip_ -e . -r requirements-dev.txt ;;
  isort) pip_ -e '.[colors]' --group dev ;;
  # flake8: tox deps
  flake8) pip_ -e . pytest coverage covdefaults ;;
  # coveragepy: tox deps (hashed pins) then the editable install, as tox does
  coveragepy)
    pip_ -r requirements/pip.txt -r requirements/pytest.txt -r requirements/light-threads.txt
    pip_ -e '.[toml]'
    .venv/bin/python igor.py zip_mods >/dev/null ;;
  arrow) pip_ -e . -r requirements/requirements-tests.txt ;;
  dateutil) pip_ -e . -r requirements-dev.txt ;;
  # more-itertools: CI runs unittest; pytest added to collect the same tests
  more-itertools) pip_ -e . pytest ;;
  boltons) pip_ -e . --group dev ;;
  # poetry: [tool.poetry.group.test] (uv cannot read poetry groups)
  poetry) pip_ -e . coverage dulwich responses jaraco-classes 'pytest>=9.0' pytest-mock pytest-randomly 'pytest-xdist[psutil]' ;;
  # black: tox dependency group cov-tests, then .[d] and .[jupyter]
  black) pip_ -e '.[d,jupyter]' --group cov-tests ;;
  alembic) pip_ -e '.[tz]' --group tests ;;
  # networkx: CI pip install .[default,test] (+ test-extras where available)
  networkx) pip_ -e '.[default,test,test-extras]' ;;
  # scrapy: tox [test-requirements] deps
  scrapy) pip_ -e . attrs 'coverage>=7.10.6' 'pexpect>=4.8.0' psutil 'pyftpdlib>=2.0.1' pygments 'pytest>=8.4.1' pytest-rerunfailures pytest-timeout pytest-xdist 'sybil>=1.3.0' 'pytest-twisted>=1.14.3' ;;
  anyio) pip_ -e '.[trio]' --group test ;;
  trio) pip_ -e . -r test-requirements.txt ;;
  # pygments: tox deps, use_develop
  pygments) pip_ -e . 'pytest~=8.2' pytest-randomly wcag-contrast-ratio ;;
  pandas)
    # pandas CI: meson-python editable build without build isolation, test and
    # optional dependencies from requirements-dev.txt (generated from
    # environment.yml; code-checks.yml installs it with pip). versioneer needs
    # a reachable tag, so deepen the clone to the latest vX.Y.0.dev0 tag on
    # main (else pandas is 0+untagged and pyarrow/xarray disown it).
    tag=$(git ls-remote --tags origin 'v*.dev0' | awk '{print $2}' | grep -v '\^{}' | sed 's#refs/tags/##' | sort -V | tail -1)
    if ! git describe --tags --match 'v[0-9]*' >/dev/null 2>&1; then
      git fetch --quiet --depth 1 origin tag "$tag"
      git fetch --quiet --shallow-since="$(git log -1 --format=%cI "$tag^{}")" origin main
    fi
    pip_ -r requirements-dev.txt
    rm -rf build
    pip_ --no-build-isolation --reinstall-package pandas -e . ;;
esac
pip_ pytest-cov

# Collection must work.
set +e
# coverage.py's addopts use --failed-first, which needs the cacheprovider.
nocache=(-p no:cacheprovider); [[ $name == coveragepy ]] && nocache=()
.venv/bin/python -m pytest -q ${nocache[@]+"${nocache[@]}"} -x --co > "$LOGS/co-$name.txt" 2>&1
status=$?
set -e
echo "$name: collect exit $status, $(grep -cE '::' "$LOGS/co-$name.txt") node ids; tail: $(tail -1 "$LOGS/co-$name.txt")"
exit $status
