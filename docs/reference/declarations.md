# `diffcone.toml`

Some dependencies are real but invisible to static analysis: a registry
filled at import time, a plugin resolved through entry points, a handler
named in a YAML file. Declare them in `diffcone.toml` at the repository
root:

```toml
[[edges]]
from = "pkg.registry.dispatch"
to = "pkg.handlers.json_handler"
why = "handlers register themselves through entry points"

[[edges]]
from = "pkg.cli"
to = "pkg.plugins"
```

Each `[[edges]]` entry says that `from` depends on `to`: a change to `to`
selects the targets that reach `from`, and the report explains the
selection as "declared in diffcone.toml" with your `why`.

| Key | Meaning |
|---|---|
| `from` | Required. The dependent: a symbol (`pkg.mod.func`, `pkg.mod.Class.method`) or a module or class, which stands for everything in it. |
| `to` | Required. What it depends on, named the same way. |
| `why` | Optional. Shown in the report. |

The rules:

- **Declarations only add edges**, so they can only select more: a wrong
  one costs a test that runs anyway, and none can make a plan miss.
- The file is read from both snapshots, so a declaration is versioned with
  the code it describes.
- An endpoint that exists in neither snapshot, an unknown key, or a file
  that does not parse is an **analysis error** (the plan selects everything
  and exits `1`) rather than a declaration that silently does nothing.
- A module or class endpoint expands to its members; a declaration joining
  more than 5 000 symbol pairs is refused as an error, so declare the
  symbols that depend on each other instead.
