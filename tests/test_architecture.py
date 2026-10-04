"""The layers of the package only depend on the ones below them (the diagram in ARCHITECTURE.md is this table)."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "geekmagic"

# package -> the packages it may import from
ALLOWED = {
    "app": {"device", "errors", "insights", "model", "paths", "providers", "render", "system"},
    "cli": {"device", "errors", "providers", "render"},
    "device": {"errors"},
    "insights": {"model"},
    "providers": {"errors", "insights", "model", "system"},
    "render": {"insights", "model"},
    "system": {"paths"},
    "errors": set(), "model": set(), "paths": set(),
}


def imports_of(path: Path) -> set[str]:
    """The geekmagic packages a file imports from (the first name after `geekmagic.`)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        modules = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
            modules += [f"{node.module}.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        for module in modules:
            parts = module.split(".")
            if parts[0] == "geekmagic" and len(parts) > 1:
                found.add(parts[1])
    return found


class LayeringTests(unittest.TestCase):
    def test_every_module_only_imports_from_the_layers_it_may(self):
        problems = []
        for path in sorted(PACKAGE.rglob("*.py")):
            relative = path.relative_to(PACKAGE)
            package = relative.parts[0] if len(relative.parts) > 1 else relative.stem
            if package == "__init__":
                continue
            self.assertIn(package, ALLOWED, f"{relative} is in a layer the architecture doesn't know")
            for imported in imports_of(path) - {package}:
                if imported not in ALLOWED[package]:
                    problems.append(f"{relative} imports geekmagic.{imported}")
        self.assertEqual(problems, [], "a lower layer reaches up")

    def test_the_table_has_no_dependency_cycles(self):
        done, visiting = set(), set()

        def visit(package):
            self.assertNotIn(package, visiting, f"cycle through {package}")
            if package in done:
                return
            visiting.add(package)
            for dependency in ALLOWED[package]:
                visit(dependency)
            visiting.discard(package)
            done.add(package)

        for package in ALLOWED:
            visit(package)


if __name__ == "__main__":
    unittest.main()
