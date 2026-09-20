"""Unit tests for symbol identity and reference resolution."""

from __future__ import annotations

from diffcone.indexer import build_index
from diffcone.model import Edge, SnapshotInfo
from diffcone.snapshot import Snapshot, module_name_for


def index(files: dict[str, str], roots: list[str] | None = None):
    snap = Snapshot(
        info=SnapshotInfo(revision="x", commit="x"),
        source_roots=roots or ["."],
        files={k: v.encode() for k, v in files.items()},
    )
    return build_index(snap)


def edges(idx, source: str) -> set[tuple[str, str, str]]:
    return {(e.target, e.kind, e.detail) for e in idx.edges if e.source == source}


def test_module_names_follow_source_roots():
    assert module_name_for("pkg/mod.py", ["."]) == "pkg.mod"
    assert module_name_for("pkg/__init__.py", ["."]) == "pkg"
    assert module_name_for("src/pkg/mod.py", ["src"]) == "pkg.mod"
    assert module_name_for("src/pkg/mod.py", [".", "src"]) == "pkg.mod"
    assert module_name_for("other/x.py", ["src"]) is None
    assert module_name_for("pkg/not-valid.py", ["."]) is None


def test_symbol_kinds_and_containers():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/m.py": (
                "X = 1\n\n"
                "def f():\n    pass\n\n"
                "class C:\n"
                "    Y = 2\n"
                "    def m(self):\n        pass\n"
                "    @staticmethod\n    def s():\n        pass\n"
                "    class Inner:\n        def im(self):\n            pass\n"
            ),
        }
    )
    kinds = {s.id: s.kind for s in idx.symbols.values()}
    assert kinds == {
        "pkg": "module",
        "pkg.m": "module",
        "pkg.m.f": "function",
        "pkg.m.C": "class",
        "pkg.m.C.m": "method",
        "pkg.m.C.s": "method",
        "pkg.m.C.Inner": "class",
        "pkg.m.C.Inner.im": "method",
    }
    assert idx.symbols["pkg.m.C.Inner.im"].container == "pkg.m.C.Inner"
    assert ("pkg.m.C", "defined_in", "") in edges(idx, "pkg.m.C.m")


def test_relative_imports_and_self_resolution():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "LIMIT = 3\n\ndef helper():\n    pass\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": (
                "from ..util import helper, LIMIT\n"
                "from .. import util\n"
                "from . import sibling\n\n"
                "class C:\n"
                "    ATTR = 1\n"
                "    def a(self):\n        return self.b() + self.ATTR + self.missing\n"
                "    def b(self):\n        return helper() + LIMIT + util.LIMIT + sibling.s()\n"
                "    @staticmethod\n    def st(self):\n        return self.b()\n"
            ),
            "pkg/sub/sibling.py": "def s():\n    pass\n",
        }
    )
    assert edges(idx, "pkg.sub.mod.C.a") == {
        ("pkg.sub.mod.C", "defined_in", ""),
        ("pkg.sub.mod.C.b", "references", ""),
        ("pkg.sub.mod.C", "references", "attribute:ATTR"),
    }
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.sub.mod.C.a"} == {
        ("attribute", "missing")
    }
    assert edges(idx, "pkg.sub.mod.C.b") == {
        ("pkg.sub.mod.C", "defined_in", ""),
        ("pkg.util.helper", "references", ""),
        ("pkg.util", "references", "attribute:LIMIT"),
        ("pkg.sub.sibling.s", "references", ""),
    }
    # staticmethod: ``self`` is an ordinary parameter, so self.b is unresolved.
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.sub.mod.C.st"} == {
        ("attribute", "b")
    }
    # Module-level imports produce init-time edges.
    assert edges(idx, "pkg.sub.mod") >= {
        ("pkg.util", "imports", ""),
        ("pkg.util.helper", "imports_name", ""),
        ("pkg.util", "imports_name", "attribute:LIMIT"),
        ("pkg.sub.sibling", "imports", ""),
    }


def test_star_imports_external_modules_and_builtins():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def exported():\n    pass\n",
            "pkg/b.py": (
                "from pkg.a import *\n"
                "import os\n"
                "from json import loads\n\n"
                "def f():\n"
                "    return exported(), os.path.join('a'), loads('1'), len([]), unknown()\n"
            ),
        }
    )
    assert edges(idx, "pkg.b.f") == {
        ("pkg.b", "defined_in", ""),
        ("pkg.a.exported", "references", ""),
    }
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.b.f"} == {
        ("name", "unknown")
    }
    assert {e.module for e in idx.external if e.symbol == "pkg.b.f"} == {"os", "json"}


def test_dynamic_and_literal_reflection():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def target():\n    pass\n",
            "pkg/b.py": (
                "import importlib\nimport pkg.a\n\n"
                "def literal():\n    importlib.import_module('pkg.a'); getattr(pkg.a, 'target')\n\n"
                "def dynamic(name):\n    importlib.import_module(name); getattr(pkg.a, name)\n"
            ),
        }
    )
    assert ("pkg.a", "imports", "") in edges(idx, "pkg.b.literal")
    assert ("pkg.a.target", "references", "") in edges(idx, "pkg.b.literal")
    assert not [u for u in idx.unresolved if u.symbol == "pkg.b.literal"]
    assert {u.detail for u in idx.unresolved if u.symbol == "pkg.b.dynamic"} == {
        "importlib.import_module(<non-literal>)",
        "getattr(<non-literal>)",
    }


def test_definition_vs_body_hash():
    a = index({"m.py": "def f(x=1):\n    return x\n"}).symbols["m.f"]
    b = index({"m.py": "def f(x=2):\n    return x\n"}).symbols["m.f"]
    c = index({"m.py": "@dec\ndef f(x=1):\n    return x\n"}).symbols["m.f"]
    d = index({"m.py": "def f(x=1):\n    return x + 0\n"}).symbols["m.f"]
    assert a.body_hash == b.body_hash == c.body_hash
    assert a.definition_hash != b.definition_hash
    assert a.definition_hash != c.definition_hash
    assert a.definition_hash == d.definition_hash and a.body_hash != d.body_hash

    m1 = index({"m.py": "import os\nX = 1\ndef f():\n    pass\n"}).symbols["m"]
    m2 = index({"m.py": "import os\nX = 1\ndef f():\n    pass\ndef g():\n    pass\n"}).symbols["m"]
    m3 = index({"m.py": "import sys\nX = 1\ndef f():\n    pass\n"}).symbols["m"]
    m4 = index({"m.py": "import os\nX = 2\ndef f():\n    pass\n"}).symbols["m"]
    assert m1.body_hash == m2.body_hash and m1.definition_hash == m2.definition_hash
    assert m1.definition_hash != m3.definition_hash and m1.body_hash == m3.body_hash
    assert m1.body_hash != m4.body_hash and m1.definition_hash == m4.definition_hash


def test_parse_error_is_recorded_not_hidden():
    idx = index({"ok.py": "def f():\n    pass\n", "bad.py": "def (:\n"})
    assert [e.path for e in idx.errors] == ["bad.py"]
    assert idx.failed_modules == {"bad"}
    assert "ok.f" in idx.symbols


def test_edge_type_is_hashable_and_ordered():
    assert Edge("a", "b", "references") < Edge("a", "c", "references")


def test_star_import_order_prefers_in_scope_modules():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def helper():\n    pass\n",
            "pkg/b.py": (
                "from numpy import *\nfrom pkg.a import *\n\ndef f():\n    helper(); array()\n"
            ),
        }
    )
    assert ("pkg.a.helper", "references", "") in edges(idx, "pkg.b.f")
    assert {e.module for e in idx.external if e.symbol == "pkg.b.f"} == {"numpy"}
    assert not [u for u in idx.unresolved if u.symbol == "pkg.b.f"]


def test_call_result_attribute_is_recorded():
    idx = index(
        {"m.py": "class Foo:\n    def run(self):\n        pass\n\ndef f():\n    Foo().run()\n"}
    )
    assert ("m.Foo", "references", "") in edges(idx, "m.f")
    assert {(u.kind, u.name, u.detail) for u in idx.unresolved if u.symbol == "m.f"} == {
        ("attribute", "run", "<expr>.run")
    }


def test_nested_scopes_do_not_leak_bindings():
    idx = index(
        {
            "m.py": (
                "def helper():\n    pass\n\n"
                "def f():\n"
                "    a = [helper for helper in range(2)]\n"
                "    g = lambda helper: helper\n"
                "    def inner(helper):\n        return helper\n"
                "    class K:\n        helper = 1\n"
                "    return helper()\n\n"
                "def shadowed():\n    helper = 1\n    return helper\n"
            )
        }
    )
    assert ("m.helper", "references", "") in edges(idx, "m.f")
    assert ("m.helper", "references", "") not in edges(idx, "m.shadowed")
    assert not [u for u in idx.unresolved if u.symbol in ("m.f", "m.shadowed")]


def test_shadowed_builtins_are_not_dynamic():
    idx = index(
        {
            "m.py": (
                "def vars(x):\n    return x\n\n"
                "def f(o):\n    return vars(o)\n\n"
                "def g(o):\n    return globals()\n"
            )
        }
    )
    assert ("m.vars", "references", "") in edges(idx, "m.f")
    assert not [u for u in idx.unresolved if u.symbol == "m.f"]
    assert [u.kind for u in idx.unresolved if u.symbol == "m.g"] == ["dynamic"]


def test_module_collector_skips_nested_definitions():
    idx = index(
        {
            "m.py": (
                "import sys\n\n"
                "def a():\n    pass\n\n"
                "if sys.platform:\n"
                "    def c(x):\n        return a() + x\n"
            )
        }
    )
    assert edges(idx, "m") == set()
    assert not [u for u in idx.unresolved if u.symbol == "m"]
    assert ("m.a", "references", "") in edges(idx, "m.c")
