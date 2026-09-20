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
        "pkg.m.X": "variable",
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
        ("pkg.util.LIMIT", "references", ""),
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
        ("pkg.util.LIMIT", "imports_name", ""),
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
    # ``X`` is a variable symbol of its own: the module body hash ignores it.
    assert m1.body_hash == m4.body_hash and m1.definition_hash == m4.definition_hash
    x1 = index({"m.py": "import os\nX = 1\ndef f():\n    pass\n"}).symbols["m.X"]
    x4 = index({"m.py": "import os\nX = 2\ndef f():\n    pass\n"}).symbols["m.X"]
    assert x1.body_hash != x4.body_hash


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


def test_inherited_attributes_resolve_through_mro():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/base.py": (
                "class Root:\n"
                "    LIMIT = 1\n"
                "    def root_m(self):\n        pass\n"
                "    def shared(self):\n        pass\n\n"
                "class Left(Root):\n"
                "    def shared(self):\n        pass\n\n"
                "class Right(Root):\n"
                "    def right_m(self):\n        pass\n"
            ),
            "pkg/sub.py": (
                "from pkg.base import Left, Right\n"
                "from external import Mixin\n\n"
                "class Child(Left, Right):\n"
                "    def m(self):\n"
                "        return (self.root_m(), self.shared(), self.right_m(),\n"
                "                self.LIMIT, self.nope)\n"
                "    def s(self):\n        return super().shared(), super().m()\n"
                "    @classmethod\n    def c(cls):\n        return cls.right_m\n\n"
                "class Mixed(Mixin, Left):\n"
                "    def m(self):\n        return self.shared(), self.from_mixin()\n\n"
                "class Loop(Loop):\n"
                "    def m(self):\n        return self.x\n\n"
                "def f():\n    return Child.root_m, Child().right_m()\n"
            ),
        }
    )
    assert edges(idx, "pkg.sub.Child.m") == {
        ("pkg.sub.Child", "defined_in", ""),
        ("pkg.base.Root.root_m", "references", ""),
        ("pkg.base.Left.shared", "references", ""),  # Left precedes Root in the MRO
        ("pkg.base.Right.right_m", "references", ""),
        ("pkg.base.Root", "references", "attribute:LIMIT"),
    }
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.sub.Child.m"} == {
        ("attribute", "nope")
    }
    # super(): next definition after Child; super().m() has no next definition.
    assert edges(idx, "pkg.sub.Child.s") == {
        ("pkg.sub.Child", "defined_in", ""),
        ("pkg.base.Left.shared", "references", ""),
    }
    assert {
        (u.kind, u.name, u.detail) for u in idx.unresolved if u.symbol == "pkg.sub.Child.s"
    } == {("attribute", "m", "super().m")}
    assert ("pkg.base.Right.right_m", "references", "") in edges(idx, "pkg.sub.Child.c")
    # An external base precedes Left: the hit is recorded but stays name-bounded,
    # because Mixin could override ``shared``; unknown names are name-bounded only.
    assert ("pkg.base.Left.shared", "references", "") in edges(idx, "pkg.sub.Mixed.m")
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.sub.Mixed.m"} == {
        ("attribute", "from_mixin"),
        ("attribute", "shared"),
    }
    # Self-inheritance does not recurse forever.
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.sub.Loop.m"} == {
        ("attribute", "x")
    }
    # Class-level and call-result access use the same lookup.
    assert {
        ("pkg.base.Root.root_m", "references", ""),
        ("pkg.sub.Child", "references", ""),
    } <= edges(idx, "pkg.sub.f")


def test_mro_is_independent_of_class_id_order():
    # ``Alpha`` sorts before ``Zed``: resolving its dotted base must not freeze
    # Zed's MRO before Zed's own bases are known.
    idx = index(
        {
            "m.py": (
                "class Base:\n"
                "    class Inherited:\n        def im(self):\n            pass\n"
                "    def x(self):\n        pass\n\n"
                "class Zed(Base):\n"
                "    class Inner:\n        def inner_m(self):\n            pass\n"
                "    def f(self):\n        return self.x()\n\n"
                "class Alpha(Zed.Inner):\n"
                "    def g(self):\n        return self.inner_m()\n\n"
                "class Beta(Zed.Inherited):\n"
                "    def h(self):\n        return self.im()\n"
            )
        }
    )
    assert ("m.Base.x", "references", "") in edges(idx, "m.Zed.f")
    assert ("m.Zed.Inner.inner_m", "references", "") in edges(idx, "m.Alpha.g")
    assert ("m.Base.Inherited.im", "references", "") in edges(idx, "m.Beta.h")
    assert ("m.Base.Inherited", "references", "") in edges(idx, "m.Beta")
    assert not [u for u in idx.unresolved if u.symbol in ("m.Zed.f", "m.Alpha.g", "m.Beta.h")]


def test_nested_class_bases_use_the_enclosing_class_body():
    idx = index(
        {
            "m.py": (
                "class Base:\n    def m(self):\n        pass\n\n"
                "class Outer:\n"
                "    Alias = 3\n"
                "    class Base:\n        def other(self):\n            pass\n"
                "    class Sub(Base):\n"
                "        def f(self):\n            return self.m(), self.other()\n"
                "    class Sub2(Alias):\n"
                "        def g(self):\n            return self.m()\n"
            )
        }
    )
    assert ("m.Outer.Base", "references", "") in edges(idx, "m.Outer.Sub")
    assert ("m.Base", "references", "") not in edges(idx, "m.Outer.Sub")
    assert ("m.Outer.Base.other", "references", "") in edges(idx, "m.Outer.Sub.f")
    assert ("m.Base.m", "references", "") not in edges(idx, "m.Outer.Sub.f")
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.Outer.Sub.f"} == {
        ("attribute", "m")
    }
    # A class-level binding as base: unknown class, reference kept, lookup bounded.
    assert ("m.Outer", "references", "attribute:Alias") in edges(idx, "m.Outer.Sub2")
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.Outer.Sub2.g"} == {
        ("attribute", "m")
    }


def test_inheritance_cycle_keeps_self_first():
    idx = index(
        {
            "m.py": (
                "class A:\n    def m(self):\n        pass\n\n"
                "class B(A):\n"
                "    def m(self):\n        pass\n"
                "    def g(self):\n        return super().m(), self.m()\n\n"
                "class A(B):\n    pass\n"
            )
        }
    )
    assert edges(idx, "m.B.g") >= {
        ("m.A.m", "references", ""),  # super().m -> next after B
        ("m.B.m", "references", ""),  # self.m -> B's own
    }


def test_class_reference_reaches_the_constructor():
    idx = index(
        {
            "m.py": (
                "class Base:\n    def __init__(self):\n        pass\n\n"
                "class Foo(Base):\n    pass\n\n"
                "class Bar:\n    def __init__(self):\n        pass\n\n"
                "def f():\n    return Foo(), Bar()\n"
            )
        }
    )
    assert edges(idx, "m.f") >= {
        ("m.Foo", "references", ""),
        ("m.Base.__init__", "references", "constructor"),
        ("m.Bar", "references", ""),
        ("m.Bar.__init__", "references", "constructor"),
    }
    assert ("m.Base.__init__", "references", "constructor") in edges(idx, "m.Foo")


def test_getattr_and_import_module_with_bounded_names():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def target():\n    pass\n\n\ndef other():\n    pass\n",
            "pkg/b.py": (
                "import importlib\nimport pkg.a\n\n"
                "NAMES = ('target', 'other')\n"
                "MODS = ['pkg.a']\n\n"
                "def loop(stmt):\n"
                "    for attr in ('body', 'orelse'):\n"
                "        getattr(stmt, attr, None)\n\n"
                "def const():\n"
                "    name = 'target'\n"
                "    return getattr(pkg.a, name)\n\n"
                "def module_level():\n"
                "    return [getattr(pkg.a, n) for n in NAMES]\n\n"
                "def mods():\n"
                "    for m in MODS:\n"
                "        importlib.import_module(m)\n\n"
                "def unbounded(name):\n"
                "    return getattr(pkg.a, name)\n\n"
                "def rebound():\n"
                "    name = 'target'\n"
                "    name = compute()\n"
                "    return getattr(pkg.a, name)\n"
            ),
        }
    )
    dyn = {u.symbol for u in idx.unresolved if u.kind == "dynamic"}
    assert dyn == {"pkg.b.unbounded", "pkg.b.rebound"}
    assert {(u.kind, u.name) for u in idx.unresolved if u.symbol == "pkg.b.loop"} == {
        ("attribute", "body"),
        ("attribute", "orelse"),
    }
    assert ("pkg.a.target", "references", "") in edges(idx, "pkg.b.const")
    assert {("pkg.a.target", "references", ""), ("pkg.a.other", "references", "")} <= edges(
        idx, "pkg.b.module_level"
    )
    assert ("pkg.a", "imports", "") in edges(idx, "pkg.b.mods")


def test_parameter_driven_getattr_uses_call_site_literals():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/mods.py": "def target():\n    pass\n",
            "pkg/m.py": (
                "import importlib\nimport pkg.mods\n\n"
                "def helper(stream, attr, value=None):\n"
                "    return getattr(stream, attr, None) == value\n\n"
                "def a(s):\n    return helper(s, 'encoding')\n\n"
                "def b(s):\n    return helper(s, attr='errors')\n\n"
                "class K:\n"
                "    def m(self, name):\n        return getattr(pkg.mods, name)\n"
                "    def caller(self):\n        return self.m('target')\n\n"
                "def with_default(attr='mode'):\n    return getattr(object(), attr)\n\n"
                "def d():\n    return with_default()\n\n"
                "def loader(mod):\n    return importlib.import_module(mod)\n\n"
                "def e():\n    return loader('pkg.mods')\n\n"
                "def escaping(obj, attr):\n    return getattr(obj, attr)\n\n"
                "def f(x):\n    fn = escaping\n    return fn(x, 'y')\n\n"
                "def unbounded_site(obj, attr):\n    return getattr(obj, attr)\n\n"
                "def g(x, n):\n    return unbounded_site(x, n)\n\n"
                "def uncalled(obj, attr):\n    return getattr(obj, attr)\n"
            ),
        }
    )
    unresolved = lambda sym: {(u.kind, u.name) for u in idx.unresolved if u.symbol == sym}  # noqa: E731
    assert unresolved("pkg.m.helper") == {("attribute", "encoding"), ("attribute", "errors")}
    assert ("pkg.mods.target", "references", "") in edges(idx, "pkg.m.K.m")
    assert ("dynamic", "") not in unresolved("pkg.m.K.m")
    assert unresolved("pkg.m.with_default") == {("attribute", "mode")}
    assert ("pkg.mods", "imports", "") in edges(idx, "pkg.m.loader")
    assert ("dynamic", "") not in unresolved("pkg.m.loader")
    # Still dynamic: escaping as a value, an unbounded call site, never called.
    for sym in ("pkg.m.escaping", "pkg.m.unbounded_site", "pkg.m.uncalled"):
        assert ("dynamic", "") in unresolved(sym), sym


def test_dict_literal_keys_bound_loop_variables():
    idx = index(
        {
            "m.py": (
                "WRAPPERS = {'assert_called': 1, 'assert_any_call': 2}\n\n"
                "def f(mod):\n"
                "    for method, wrapper in WRAPPERS.items():\n"
                "        getattr(mod.NonCallableMock, method)\n"
                "    for key in WRAPPERS:\n"
                "        getattr(mod, key)\n"
                "    for k2 in WRAPPERS.keys():\n"
                "        getattr(mod, k2)\n\n"
                "def g(mod):\n"
                "    for method, wrapper in WRAPPERS.items():\n"
                "        getattr(mod, wrapper)\n\n"
                "def h(mod, d):\n"
                "    for k in d.items():\n"
                "        getattr(mod, k)\n"
            )
        }
    )
    f_refs = {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.f"}
    assert f_refs == {
        ("attribute", "NonCallableMock"),  # the receiver chain on a local
        ("attribute", "assert_called"),
        ("attribute", "assert_any_call"),
    }
    assert ("dynamic", "") in {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.g"}
    assert ("dynamic", "") in {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.h"}


def test_literal_bindings_follow_source_order():
    idx = index(
        {
            "m.py": (
                "def f(mod):\n"
                "    wrappers = {'assert_called': 1, 'assert_any_call': 2}\n"
                "    for method, wrapper in wrappers.items():\n"
                "        getattr(mod, method)\n\n"
                "def g(mod):\n"
                "    names = ('a', 'b')\n"
                "    for n in names:\n"
                "        getattr(mod, n)\n"
            )
        }
    )
    for sym in ("m.f", "m.g"):
        refs = {(u.kind, u.name) for u in idx.unresolved if u.symbol == sym}
        assert ("dynamic", "") not in refs, sym
    assert {u.name for u in idx.unresolved if u.symbol == "m.f"} == {
        "assert_called",
        "assert_any_call",
        "items",  # ``wrappers.items`` on a local is itself a bounded attribute
    }


def test_dict_items_only_bound_for_pair_targets():
    idx = index(
        {
            "m.py": (
                "D = {'a': 1}\n\n"
                "def single(mod):\n"
                "    for pair in D.items():\n        getattr(mod, pair)\n\n"
                "def keys(mod):\n"
                "    for k in D.keys():\n        getattr(mod, k)\n"
            )
        }
    )
    assert ("dynamic", "") in {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.single"}
    assert {u.name for u in idx.unresolved if u.symbol == "m.keys"} == {"a"}


def test_receiver_lookups_record_in_scope_overrides():
    idx = index(
        {
            "m.py": (
                "class Base:\n"
                "    def run(self):\n        return self.step(), Base.step(self), super().step()\n"
                "    def step(self):\n        return 0\n\n"
                "class Sub(Base):\n"
                "    def step(self):\n        return 1\n\n"
                "class Other(Base):\n    pass\n\n"
                "class Deep(Sub):\n"
                "    def step(self):\n        return 2\n\n"
                "def f(b):\n    return b.step()\n"
            )
        }
    )
    run = edges(idx, "m.Base.run")
    assert ("m.Base.step", "references", "") in run
    assert ("m.Sub.step", "references", "override") in run
    assert ("m.Deep.step", "references", "override") in run
    assert not any(t.startswith("m.Other") for t, _, _ in run)
    # Explicit class access and super() do not dispatch: only one override
    # pair exists, from the ``self.step()`` call.
    assert sum(1 for _, _, d in run if d == "override") == 2
    # An unknown receiver stays name-bounded, not dispatched.
    assert {u.name for u in idx.unresolved if u.symbol == "m.f"} == {"step"}


def test_overrides_include_mixins_and_class_attribute_rebindings():
    idx = index(
        {
            "m.py": (
                "class Mixin:\n    def step(self):\n        return 'mixin'\n\n"
                "class Base:\n"
                "    hook = None\n"
                "    def run(self):\n        return self.step(), self.hook\n"
                "    def step(self):\n        return 0\n\n"
                "class ViaMixin(Mixin, Base):\n    pass\n\n"
                "class HookMethod(Base):\n    def hook(self):\n        return 1\n\n"
                "class Rebound(Base):\n    step = Mixin.step\n\n"
                "class Plain(Base):\n    pass\n"
            )
        }
    )
    run = edges(idx, "m.Base.run")
    assert ("m.Mixin.step", "references", "override") in run  # inherited from a mixin
    assert ("m.HookMethod.hook", "references", "override") in run  # attribute -> method
    assert ("m.Rebound", "references", "override:attribute:step") in run  # rebinding
    assert not any(t.startswith("m.Plain") for t, _, _ in run)


def test_dispatched_call_sites_reach_overrides():
    idx = index(
        {
            "m.py": (
                "class Base:\n"
                "    def run(self, mode):\n        return self.step(mode)\n"
                "    def step(self, name):\n        return name\n\n"
                "class Sub(Base):\n"
                "    def step(self, name):\n        return getattr(self, name)\n\n"
                "def direct():\n    return Sub().step('a')\n"
            )
        }
    )
    # Sub.step's getattr is reached through Base.run's unbounded ``mode`` as
    # well as the literal direct call, so it must stay dynamic.
    assert ("dynamic", "") in {(u.kind, u.name) for u in idx.unresolved if u.symbol == "m.Sub.step"}


def test_module_level_variables_are_symbols():
    idx = index(
        {
            "pkg/__init__.py": "from pkg._make import attrib\n\nib = attrib\n__all__ = ['ib']\n",
            "pkg/_make.py": "def attrib():\n    pass\n",
            "pkg/config.py": (
                "LIMIT = 3\n"
                "PAIR_A, PAIR_B = 1, 2\n"
                "REBOUND = 1\n"
                "REBOUND = 2\n"
                "if LIMIT:\n    IN_BLOCK = 5\n"
                "def helper():\n    return LIMIT + PAIR_A + REBOUND + IN_BLOCK\n"
            ),
            "pkg/user.py": (
                "import pkg\nfrom pkg import config\nfrom pkg.config import LIMIT\n\n"
                "def f():\n    return pkg.ib(), config.LIMIT, LIMIT\n"
            ),
        }
    )
    kinds = {i: s.kind for i, s in idx.symbols.items() if i.startswith("pkg.config")}
    assert kinds["pkg.config.LIMIT"] == "variable"
    assert "pkg.config.PAIR_A" not in kinds and "pkg.config.REBOUND" not in kinds
    assert "pkg.config.IN_BLOCK" not in kinds
    assert idx.symbols["pkg.config.LIMIT"].container == "pkg.config"
    # References resolve to the variable, not the module; other bindings stay on it.
    assert edges(idx, "pkg.config.helper") == {
        ("pkg.config", "defined_in", ""),
        ("pkg.config.LIMIT", "references", ""),
        ("pkg.config", "references", "attribute:PAIR_A"),
        ("pkg.config", "references", "attribute:REBOUND"),
        ("pkg.config", "references", "attribute:IN_BLOCK"),
    }
    assert edges(idx, "pkg.user.f") >= {
        ("pkg.ib", "references", ""),
        ("pkg.config.LIMIT", "references", ""),
    }
    # An alias variable depends on what it aliases; the module does not.
    assert edges(idx, "pkg.ib") == {
        ("pkg", "defined_in", ""),
        ("pkg._make.attrib", "references", ""),
    }
    assert ("pkg._make.attrib", "references", "") not in edges(idx, "pkg")
    # Module-level from-imports of a variable are imports_name edges to it.
    assert ("pkg.config.LIMIT", "imports_name", "") in edges(idx, "pkg.user")


def test_defaults_resolve_in_the_enclosing_scope_and_alias_variables():
    idx = index(
        {
            "m.py": (
                "INFO = {'a': 1}\n"
                "REG = {}\n"
                "REG['seed'] = 0\n\n"
                "def helper():\n    pass\n\n"
                "def build(info=INFO, reg=REG, fn=helper):\n"
                "    for k in info:\n        reg[k] = info[k]\n"
                "    reg.update({})\n"
                "    info = {}  # rebinding the parameter is not a mutation of INFO\n"
                "    return fn\n\n"
                "def annotated(x: 'helper') -> helper:\n    return x\n"
            )
        }
    )
    build = edges(idx, "m.build")
    assert ("m.INFO", "references", "") in build
    assert ("m.REG", "references", "") in build
    assert ("m.helper", "references", "") in build
    assert ("m.build", "references", "mutated_by") in edges(idx, "m.REG")
    assert ("m.build", "references", "mutated_by") not in edges(idx, "m.INFO")
    assert not [u for u in idx.unresolved if u.symbol == "m.build"]
    assert ("m.helper", "references", "") in edges(idx, "m.annotated")
    # Module-level mutation lines belong to the variable for coverage.
    assert idx.symbols["m.REG"].line_ranges == ((2, 2), (3, 3))


def test_prefix_bounded_dynamic_names():
    idx = index(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def pytest_one():\n    pass\n\n\ndef other():\n    pass\n",
            "pkg/b.py": "def pytest_two():\n    pass\n",
            "pkg/lazy.py": (
                "import importlib\nfrom pkg import a\n\n\n"
                "def fstring(name):\n    return importlib.import_module(f'pkg.{name}')\n\n\n"
                "def concat(name):\n    return importlib.import_module('pkg.' + name)\n\n\n"
                "def percent(name):\n    return importlib.import_module('pkg.%s' % name)\n\n\n"
                "def fmt(name):\n    return importlib.import_module('pkg.{}'.format(name))\n\n\n"
                "def external(name):\n    return importlib.import_module(f'os.{name}')\n\n\n"
                "def unbounded(name):\n    return importlib.import_module(f'{name}.x')\n\n\n"
                "def hooks(name):\n    return getattr(a, f'pytest_{name}')\n\n\n"
                "def hooks_unknown(obj, name):\n    return getattr(obj, f'pytest_{name}')\n\n\n"
                "def const():\n    return importlib.import_module(f'pkg.a')\n"
            ),
        }
    )
    dyn = {u.symbol for u in idx.unresolved if u.kind == "dynamic"}
    assert dyn == {"pkg.lazy.unbounded"}
    for fn in ("fstring", "concat", "percent", "fmt"):
        assert {
            ("pkg.a", "imports", ""),
            ("pkg.b", "imports", ""),
            ("pkg", "imports", ""),
        } <= edges(idx, f"pkg.lazy.{fn}"), fn
    assert "os.*" in {x.module for x in idx.external if x.symbol == "pkg.lazy.external"}
    assert ("pkg.a.pytest_one", "references", "") in edges(idx, "pkg.lazy.hooks")
    assert ("pkg.a.other", "references", "") not in edges(idx, "pkg.lazy.hooks")
    assert {u.name for u in idx.unresolved if u.symbol == "pkg.lazy.hooks"} == {"pytest_two"}
    assert {u.name for u in idx.unresolved if u.symbol == "pkg.lazy.hooks_unknown"} == {
        "pytest_one",
        "pytest_two",
    }
    assert ("pkg.a", "imports", "") in edges(idx, "pkg.lazy.const")
