# Test environments for the evaluation corpus

Built 2026-10-11 for the 42 census repositories (`scripts/census.py` REPOS
at diffcone 312bce1) and pandas. The scripts are beside this file; what they
build lives under the work directory `E` (an environment variable, in the
session scratchpad):

* `repos/<name>`: clone (`--depth 40 --no-tags --single-branch`; pandas
  deepened to its `v3.2.0.dev0` tag), env in `repos/<name>/.venv`.
* `fixenv.sh <name>`: recreates one env from scratch (clones if missing),
  then checks `pytest -q -p no:cacheprovider -x --co`. `fixenv.sh --list`
  lists the names. The recipe per repo is the `case` in that script; the
  table below is the same recipe in words. `one-env.sh <name>` wraps it with
  a log (`$E/logs/env-<name>.log`); all at once:
  `xargs -P 6 -n1 ./one-env.sh < names.txt` (about 2 minutes on a warm uv
  cache; pandas about 2 more for the meson build).
* `check-one.sh <name> <tree>`: the fixture and collection checks run from
  a diffcone tree, into `$E/out/<name>.{fx,cc}.txt`; `checks_table.py $E`
  tabulates them.
* `suite-one.sh <name>`: one full run of the suite (15 min cap) into
  `$E/logs/suite-<name>.txt`.

## Common to every env

* `uv venv -p 3.13` (uv's CPython 3.13.3), then `uv pip install -e .` with
  the test dependencies the project's CI installs, plus `pytest-cov`.
* **Version pretending.** The clones have no tags, so setuptools-scm and
  hatch-vcs would version a project `0.1.devN`, and the resolver then picks
  ancient releases of whatever requires it (pluggy got pytest-benchmark 3.x
  with `config.warn`; dateutil got freezegun 0.0.2). `fixenv.sh` exports
  `SETUPTOOLS_SCM_PRETEND_VERSION` = the project's latest release on PyPI
  (name from `[project]` or `setup.cfg`); projects with a static version
  ignore it.
* **Running a suite** (not needed for the checks): some suites start the
  project's console scripts by name (coverage.py's `coverage`, tox's
  `tombi`, python-dotenv's `dotenv`), as tox and `uv run` allow because they
  activate the env. Put `repos/<name>/.venv/bin` first on `PATH`.
* coverage.py's `addopts` use `--failed-first`, and urllib3's
  pytest-memray `limit_memory` marks, need the cache plugin: never pass
  `-p no:cacheprovider` in a command for these. `validate`/`corpus` keep
  the plugin on an empty cache of their own, so no command needs it.
* pre-commit's `tests/languages` builds language runtimes from source (a
  Ruby build spins forever on macOS); its own tox ignores the directory:
  add `--ignore=tests/languages` to its command.

## Per repository

Status: **ok** = collection works and the suite runs (the failures listed
are environment-specific: macOS, paths, network, missing system tools; none
blocks either check). No repository is collects-only or broken.
Suite = one run of `pytest -q -p no:cacheprovider` at the HEAD below.

| repo | HEAD | recipe (after `uv venv -p 3.13`; all `+ pytest-cov`) | status | suite |
|---|---|---|---|---|
| flask | d086db856b | `-e .[async,dotenv] --group tests` | ok | 494 passed |
| jinja | 5ef70112a1 | `-e .[i18n] --group tests` | ok | 911 passed |
| werkzeug | 3e430f6890 | `-e .[watchdog] --group tests` | ok | 1065 passed, 1 failed (AF_UNIX path too long under /private/tmp) |
| itsdangerous | 672971d66a | `-e . --group tests` | ok | 297 passed |
| httpx | b5addb64f0 | `-r requirements.txt` (it holds `-e .[brotli,cli,http2,socks,zstd]`; scripts/install) | ok | 1416 passed, 1 failed |
| starlette | 96d5023c55 | `-e .[full] --group dev` (scripts/install = `uv sync --frozen`) | ok | 1347 passed |
| uvicorn | 07bd5d4c94 | `-e .[standard] --group dev` (scripts/install = `uv sync --frozen`) | ok | 1366 passed |
| requests | 611c6162cb | `-e .[socks] --group test` | ok | 619 passed |
| urllib3 | 5ebe9bf9f6 | `-e .[socks,brotli,zstd,h2] --group dev` (noxfile test session) | ok | 2496 passed (with the cacheprovider; see above) |
| rich | 9d8f9a372c | `-e . pytest attrs typing-extensions` (poetry dev-dependencies) | ok | 947 passed, 9 failed (newer Pygments renders differently) |
| typer | b15210b71a | `-e . --group tests` | ok | 1468 passed |
| fastapi | f5c6e9b4f9 | `-e .[all] --group tests` | ok | 3596 passed |
| pydantic | 0edb9347fa | `-e .[email,timezone] --group dev --group testing-extra --group pydantic-core/pyproject.toml:testing-extra` (uv workspace: pydantic-core is built from source by maturin, needs `cargo`) | ok | 12972 passed, 25 failed (all `tests/pydantic_core/test_docstrings.py`, which CI ignores) |
| marshmallow | d08471b178 | `-e . --group tests` | ok | 1190 passed |
| cattrs | 73fa273b12 | `-e .[ujson,orjson,msgpack,pyyaml,tomlkit,cbor2,bson,msgspec] --group test --group lint` (justfile: all extras) | ok | 1121 passed, 2 failed (tomllib converter) |
| tenacity | 58c45dc02b | `-e .[test] --group dev` (CI: `uv run poe`) | ok | 185 passed |
| python-dotenv | 0b28805917 | `-e .[cli] pytest click ipython` (tox deps) | ok | 268 passed, 20 failed (`dotenv` not on PATH; see above) |
| pluggy | 87f130aa18 | `-e . --group testing` (needs the version pretending) | ok | 219 passed |
| pytest-xdist | 4616feb8da | `-e .[testing,psutil,setproctitle] pytest` (tox extras) | ok | 236 passed |
| pytest-asyncio | b12808bd8d | `-e . --group test` (tox dependency_groups) | ok | 294 passed, 1 failed |
| pip | a7002c9771 | `-e . --group test`, then `.venv/bin/python -m pip wheel -w tests/data/common_wheels --group test-common-wheels` (nox does the same) | ok | with `-n 6`: 3080 passed, 29 failed (28 in test_completion: shell completion; serially it runs over 15 min). Without common_wheels, 1148 errors |
| packaging | 332d716676 | `-e . --group test` | ok | 62491 passed |
| build | 832f489a6d | `-e .[uv,virtualenv] --group test` (tox) | ok | 312 passed, 4 failed (`_importlib` NameError in a cycle test) |
| twine | f536ac5d0c | `-e .[keyring] pretend pytest pytest-socket coverage build` (tox deps) | ok | 224 passed, 1 failed (readme check) |
| virtualenv | bf09c7eb62 | `-e . --group test` | ok | 524 passed |
| tox | 55e560bfcc | `-e .[completion] --group test` (tox.toml) | ok | 8563 passed, 7 errors (`tombi` not on PATH) |
| pre-commit | 9ad8a0a99a | `-e . -r requirements-dev.txt` | ok | 823 passed |
| isort | 92c5685543 | `-e .[colors] --group dev` | ok | 686 passed |
| flake8 | 229f6c5387 | `-e . pytest coverage covdefaults` (tox deps) | ok | 467 passed |
| coveragepy | 0287d5da35 | `-r requirements/pip.txt -r requirements/pytest.txt -r requirements/light-threads.txt` (hashed pins), then `-e .[toml]` (builds the C tracer), then `python igor.py zip_mods` | ok | 1639 passed with `.venv/bin` on PATH (130 fail without it: `coverage: command not found`) |
| arrow | 2224255c4a | `-e . -r requirements/requirements-tests.txt` (tox) | ok | 1902 passed |
| dateutil | 2642afacc3 | `-e . -r requirements-dev.txt` (needs the version pretending) | ok | 1991 passed, 41 failed (tz database warnings on macOS) |
| more-itertools | b21ec4ee8e | `-e . pytest` (CI runs unittest; pytest collects the same) | ok | 768 passed |
| boltons | 4e5faa3d7e | `-e . --group dev` (tox) | ok | 525 passed |
| poetry | 63bba274f8 | `-e . coverage dulwich responses jaraco-classes "pytest>=9.0" pytest-mock pytest-randomly "pytest-xdist[psutil]"` (`[tool.poetry.group.test]`, which uv cannot read) | ok | 3300 passed, 11 failed (Python discovery on this machine) |
| black | 11cff1312b | `-e .[d,jupyter] --group cov-tests` (tox) | ok | 663 passed |
| alembic | 1bc21b86dd | `-e .[tz] --group tests` | ok | 1928 passed |
| networkx | 6da4704cbf | `-e .[default,test,test-extras]` | ok | 9329 passed |
| scrapy | be7bdc4cd1 | `-e . attrs coverage pexpect psutil pyftpdlib pygments pytest pytest-rerunfailures pytest-timeout pytest-xdist sybil pytest-twisted` (tox `[test-requirements]`; no httpx/aiohttp extras, so their handler tests skip) | ok | 6125 passed, 5 failed (Sybil doc examples needing modules not installed) |
| anyio | c326ee1f60 | `-e .[trio] --group test` | ok | 4071 passed |
| trio | bcc9ceeb8b | `-e . -r test-requirements.txt` | ok | 823 passed |
| pygments | 156d85fdd1 | `-e . "pytest~=8.2" pytest-randomly wcag-contrast-ratio` (tox deps) | ok | 5330 passed |
| pandas | bc2fe1b0a0 | see below | ok | smoke: `pandas/tests/series tools io/parser -n 4 -m "not slow and not network and not db and not single_cpu"`: 21839 passed |

### pandas

pandas' main CI uses pixi now; the pip translation (what `code-checks.yml`
does) works on Python 3.13:

```bash
cd repos/pandas
# versioneer needs a reachable tag, else pandas is 0+untagged and pyarrow
# disowns it ("pyarrow requires pandas 2.2.2 or above"):
git fetch --depth 1 origin tag v3.2.0.dev0
git fetch --shallow-since="$(git log -1 --format=%cI 'v3.2.0.dev0^{}')" origin main
uv venv -p 3.13 .venv
uv pip install -p .venv/bin/python -r requirements-dev.txt   # all of it, PyQt5 included
rm -rf build                                                 # stale meson dir keeps the old version
uv pip install -p .venv/bin/python --no-build-isolation --reinstall-package pandas -e .
```

The editable build takes about 1.5 minutes (meson, 8 cores) and rebuilds on
import after a C/Cython change. `pytest --co` collects 262 583 cases.
