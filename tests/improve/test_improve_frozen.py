"""Acceptance 8: the improvement loop has no write path to the frozen layer (research §1 rule 4, §3.3 R3).

Static: every file-system write in dh/improve/*.py sits in ONE guarded function, nothing shells out or copies files,
and no improvement module names a frozen path except gates.py (which is frozen itself and only reads).
Runtime: the guarded writer refuses every frozen path (tests/improve/test_improve_playbook.py) and the round guard
(dh.config.frozen_digest) covers gates.py and evals/.
"""
import ast
import re
from pathlib import Path

import pytest

from dh import config
from dh.improve import playbook as pbm

REPO = Path(__file__).resolve().parents[2]
IMPROVE = sorted((REPO / "dh" / "improve").glob("*.py"))
FROZEN = config.FROZEN_PATHS + config.LOCAL_FROZEN_PATHS
# a frozen path named as a path: a directory followed by "/" or the end of the string ("data, not ..." is prose)
FROZEN_RX = [(fp, re.compile(r"(?<![\w.-])" + re.escape(fp) + (r"(?:/|$)" if (REPO / fp).is_dir() else r"(?![\w])")))
             for fp in FROZEN]

WRITE_METHODS = {"write_text", "write_bytes", "unlink", "rmdir", "touch", "chmod", "symlink_to", "hardlink_to",
                 "rename", "mkdir", "cleanup"}
WRITE_FUNCS = {("os", "replace"), ("os", "rename"), ("os", "remove"), ("os", "unlink"), ("os", "makedirs"),
               ("os", "rmdir"), ("os", "system"), ("os", "popen"), ("os", "symlink"), ("os", "link"),
               ("os", "truncate"), ("os", "chmod"), ("os", "open"), ("os", "write"), ("os", "fdopen"),
               ("os", "mkdir"), ("io", "open"), ("codecs", "open"),
               ("tempfile", "mkstemp"), ("tempfile", "mkdtemp"), ("tempfile", "NamedTemporaryFile"),
               ("tempfile", "TemporaryFile"), ("tempfile", "SpooledTemporaryFile"), ("tempfile", "TemporaryDirectory")}
FORBIDDEN_IMPORTS = {"shutil", "subprocess", "pty", "evals.marks"}
# the only places allowed to write: the guarded writer, and the temp-dir creation of `materialise`
ALLOWED = {("playbook.py", "_guarded_write"): {"open", "os.replace"},
           ("playbook.py", "materialise"): {"mkdir"},
           ("playbook.py", "eval_root"): {"tempfile.TemporaryDirectory", "cleanup"}}


def _write_mode(call: ast.Call, pos: int) -> bool:
    """True unless the call's mode is a literal read-only mode ('r', 'rb', 'rt')."""
    mode = call.args[pos] if len(call.args) > pos else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False  # open() and Path.open() default to "r"
    return not (isinstance(mode, ast.Constant) and set(str(mode.value)) <= set("rbt"))


def _sinks(tree: ast.AST):
    """(function name, sink) for every write-capable call."""
    out = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name) and f.id == "open":
                    if _write_mode(child, 1):
                        out.append((fn, "open"))
                elif isinstance(f, ast.Name) and f.id in {n for _, n in WRITE_FUNCS if _ == "tempfile"}:
                    out.append((fn, f"tempfile.{f.id}"))  # from tempfile import X
                elif isinstance(f, ast.Attribute):
                    if isinstance(f.value, ast.Name) and (f.value.id, f.attr) in WRITE_FUNCS:
                        out.append((fn, f"{f.value.id}.{f.attr}"))
                    elif f.attr == "open" and _write_mode(child, 0):  # Path.open("w"), fh.open(...)
                        out.append((fn, "Path.open"))
                    elif f.attr in WRITE_METHODS:
                        out.append((fn, f.attr))
            visit(child, name)

    visit(tree, "<module>")
    return out


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


@pytest.mark.parametrize("path", IMPROVE, ids=lambda p: p.name)
def test_writes_only_through_the_guard(path):
    tree = ast.parse(path.read_text())
    for fn, sink in _sinks(tree):
        assert sink in ALLOWED.get((path.name, fn), set()), f"{path.name}:{fn} writes via {sink}"


@pytest.mark.parametrize("path", IMPROVE, ids=lambda p: p.name)
def test_no_shelling_out_or_copying(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {node.module or ""} | {f"{node.module}.{a.name}" for a in node.names}
        else:
            continue
        assert not names & FORBIDDEN_IMPORTS, f"{path.name} imports {names & FORBIDDEN_IMPORTS}"


@pytest.mark.parametrize("path", [p for p in IMPROVE if p.name != "gates.py"], ids=lambda p: p.name)
def test_no_frozen_path_literals(path):
    tree = ast.parse(path.read_text())
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            for fp, rx in FROZEN_RX:
                assert not rx.search(node.value), f"{path.name} names frozen path {fp!r} in {node.value!r}"


ROGUE = """
import io, codecs, os, tempfile
from pathlib import Path
from tempfile import mkstemp
def f(p, m):
    open(p, "w"); open(p, m); Path(p).open("a"); Path(p).open(mode="w"); io.open(p, "wb"); codecs.open(p, "w")
    os.open(p, os.O_WRONLY); os.write(3, b"x"); os.fdopen(3, "w"); mkstemp(); tempfile.NamedTemporaryFile()
    Path(p).write_text("x"); Path(p).unlink(); os.replace(p, p)
def ok(p):
    open(p); open(p, "rb"); Path(p).open(); Path(p).read_text()
"""


def test_scanner_catches_every_write_form():
    sinks = _sinks(ast.parse(ROGUE))
    assert all(fn == "f" for fn, _ in sinks)  # nothing in ok()
    assert len(sinks) == 14, sinks


def test_gates_itself_has_no_write_sink():
    tree = ast.parse((REPO / "dh" / "improve" / "gates.py").read_text())
    assert _sinks(tree) == []


def test_frozen_layer_is_guarded_at_round_time():
    assert "dh/improve/gates.py" in config.FROZEN_PATHS and "evals" in config.FROZEN_PATHS
    digest = config.frozen_digest(REPO)
    assert "dh/improve/gates.py" in digest
    assert {"evals/must_pass.jsonl", "evals/golden.jsonl", "evals/labels.py"} <= set(digest)


@pytest.mark.parametrize("rel", FROZEN)
def test_guard_refuses_every_frozen_path(tmp_path, rel):
    target = tmp_path / rel / ("x.yaml" if rel.endswith("/") or "." not in rel.split("/")[-1] else "")
    with pytest.raises(pbm.PlaybookError, match="refusing"):
        pbm._guarded_write(target, "x", allowed_root=tmp_path)
    assert not target.exists()


def test_packaging_ships_the_improvement_loop_and_evals():
    import tomllib
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]
    inc = cfg["packages"]["find"]["include"]
    assert "dh*" in inc and "evals*" in inc
    assert "*.jsonl" in cfg["package-data"]["evals"]


def test_data_dir_is_frozen():
    assert {"data", "data/taxonomy"} <= set(config.FROZEN_PATHS)
    assert any(k.startswith("data/taxonomy/") for k in config.frozen_digest(REPO))
