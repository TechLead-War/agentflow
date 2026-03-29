from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import git_ops


@dataclass
class RepoAnalysis:
    tracked_files: list[str]
    python_files: list[str]
    imports: dict[str, set[str]] = field(default_factory=dict)
    imported_by: dict[str, set[str]] = field(default_factory=dict)

    def to_prompt_context(
        self,
        *,
        max_shared_files: int = 8,
        max_neighborhoods: int = 8,
        max_neighbors_per_file: int = 5,
    ) -> str:
        """Render a compact import-graph summary for the planner prompt."""
        if not self.python_files:
            return "REPO IMPORT ANALYSIS:\n- No Python files were detected."

        edge_count = sum(len(edges) for edges in self.imports.values())
        lines = [
            "REPO IMPORT ANALYSIS:",
            (
                f"- {len(self.python_files)} Python files with {edge_count} "
                f"internal import edges."
            ),
            (
                "- Prefer task boundaries that stay within the same import "
                "neighborhood. If one task edits an imported module and another "
                "edits its importer, add a dependency or keep them together."
            ),
        ]

        shared = [
            path for path in self.python_files
            if self.imported_by.get(path)
        ]
        shared.sort(key=lambda path: (-len(self.imported_by.get(path, set())), path))
        if shared:
            lines.append("HIGH FAN-IN FILES:")
            for path in shared[:max_shared_files]:
                importers = sorted(self.imported_by.get(path, set()))
                preview = ", ".join(importers[:max_neighbors_per_file])
                if len(importers) > max_neighbors_per_file:
                    preview += f", ... ({len(importers) - max_neighbors_per_file} more)"
                lines.append(f"- {path} <- {preview}")

        neighborhoods = []
        for path in self.python_files:
            neighbors = sorted(self.imports.get(path, set()) | self.imported_by.get(path, set()))
            if neighbors:
                neighborhoods.append((path, neighbors))
        neighborhoods.sort(key=lambda item: (-len(item[1]), item[0]))

        if neighborhoods:
            lines.append("IMPORT NEIGHBORHOODS:")
            for path, neighbors in neighborhoods[:max_neighborhoods]:
                preview = ", ".join(neighbors[:max_neighbors_per_file])
                if len(neighbors) > max_neighbors_per_file:
                    preview += f", ... ({len(neighbors) - max_neighbors_per_file} more)"
                lines.append(f"- {path}: {preview}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "tracked_files": list(self.tracked_files),
            "python_files": list(self.python_files),
            "imports": {
                path: sorted(edges) for path, edges in sorted(self.imports.items())
            },
            "imported_by": {
                path: sorted(edges) for path, edges in sorted(self.imported_by.items())
            },
        }

    def apply_task_dependencies(self, tasks: list[dict]) -> list[str]:
        """Add conservative import-based dependencies between planned tasks."""
        deps = {
            task["id"]: set(task.get("depends_on", []))
            for task in tasks
            if "id" in task
        }
        task_files = {
            task["id"]: self._known_python_files(task.get("files", []))
            for task in tasks
            if "id" in task
        }

        messages: list[str] = []

        for importer_task in tasks:
            importer_id = importer_task.get("id")
            if not importer_id:
                continue
            importer_files = task_files.get(importer_id, set())
            if not importer_files:
                continue

            for imported_task in tasks:
                imported_id = imported_task.get("id")
                if not imported_id or imported_id == importer_id:
                    continue

                imported_files = task_files.get(imported_id, set())
                if not imported_files:
                    continue

                match = _find_import_match(importer_files, imported_files, self.imports)
                if not match:
                    continue

                if _has_path(deps, importer_id, imported_id):
                    continue
                if _has_path(deps, imported_id, importer_id):
                    messages.append(
                        "Skipped import-based dependency "
                        f"'{importer_id}' -> '{imported_id}' because it would create a cycle."
                    )
                    continue

                deps[importer_id].add(imported_id)
                importer_task.setdefault("depends_on", []).append(imported_id)
                importer_task["depends_on"] = sorted(set(importer_task["depends_on"]))

                src, dest = match
                messages.append(
                    "Added import-based dependency "
                    f"'{importer_id}' -> '{imported_id}' because {src} imports {dest}."
                )

        return messages

    def _known_python_files(self, files: list[str]) -> set[str]:
        known = set()
        for path in files:
            normalized = _normalize_repo_path(path)
            if normalized in self.imports or normalized in self.imported_by:
                known.add(normalized)
        return known


def analyze_repository(repo_path: str, tracked_files: list[str] | None = None) -> RepoAnalysis:
    """Build an internal import graph for tracked Python files."""
    files = tracked_files if tracked_files is not None else _list_tracked_files(repo_path)
    normalized_files = sorted({
        _normalize_repo_path(path) for path in files
        if path and not path.startswith(".git/")
    })
    python_files = [path for path in normalized_files if path.endswith(".py")]

    module_index = _build_module_index(python_files)
    imports: dict[str, set[str]] = {path: set() for path in python_files}

    for path in python_files:
        file_path = Path(repo_path) / path
        try:
            source = file_path.read_text(errors="replace")
        except OSError:
            continue

        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue

        module_name = module_index.by_path.get(path)
        if not module_name:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _resolve_absolute_import(alias.name, module_index.by_module)
                    if target and target != path:
                        imports[path].add(target)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    target = _resolve_from_import(
                        current_path=path,
                        current_module=module_name,
                        level=node.level,
                        module=node.module,
                        alias_name=alias.name,
                        by_module=module_index.by_module,
                    )
                    if target and target != path:
                        imports[path].add(target)

    imported_by: dict[str, set[str]] = {path: set() for path in python_files}
    for source, destinations in imports.items():
        for destination in destinations:
            imported_by.setdefault(destination, set()).add(source)

    return RepoAnalysis(
        tracked_files=normalized_files,
        python_files=python_files,
        imports=imports,
        imported_by=imported_by,
    )


@dataclass
class _ModuleIndex:
    by_module: dict[str, str]
    by_path: dict[str, str]


def _list_tracked_files(repo_path: str) -> list[str]:
    files = git_ops.run_git(["ls-files"], cwd=repo_path, check=False)
    if files.strip():
        return [line for line in files.splitlines() if line.strip()]

    repo_root = Path(repo_path)
    return [
        str(path.relative_to(repo_root).as_posix())
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    ]


def _build_module_index(python_files: list[str]) -> _ModuleIndex:
    by_module: dict[str, str] = {}
    by_path: dict[str, str] = {}

    for path in python_files:
        module_name = _module_name_for_path(path)
        if not module_name:
            continue
        by_module[module_name] = path
        by_path[path] = module_name

    return _ModuleIndex(by_module=by_module, by_path=by_path)


def _module_name_for_path(path: str) -> str | None:
    normalized = PurePosixPath(path)
    if normalized.suffix != ".py":
        return None

    parts = list(normalized.with_suffix("").parts)
    if not parts:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _normalize_repo_path(path: str) -> str:
    return PurePosixPath(path).as_posix()


def _resolve_absolute_import(module_name: str, by_module: dict[str, str]) -> str | None:
    for candidate in _candidate_modules(module_name):
        if candidate in by_module:
            return by_module[candidate]
    return None


def _resolve_from_import(
    *,
    current_path: str,
    current_module: str,
    level: int,
    module: str | None,
    alias_name: str,
    by_module: dict[str, str],
) -> str | None:
    package = _package_for_module(current_path, current_module)
    base_parts = package.split(".") if package else []

    if level:
        keep = len(base_parts) - level + 1
        if keep < 0:
            keep = 0
        base_parts = base_parts[:keep]

    target_parts = list(base_parts)
    if module:
        target_parts.extend(part for part in module.split(".") if part)

    base_module = ".".join(part for part in target_parts if part)
    if not module and alias_name and alias_name != "*":
        nested_module = f"{base_module}.{alias_name}" if base_module else alias_name
        if nested_module in by_module:
            return by_module[nested_module]

    for candidate in _candidate_modules(base_module, alias_name):
        if candidate in by_module:
            return by_module[candidate]
    return None


def _package_for_module(path: str, module_name: str) -> str:
    if path.endswith("/__init__.py") or path == "__init__.py":
        return module_name
    if "." not in module_name:
        return ""
    return module_name.rsplit(".", 1)[0]


def _candidate_modules(base_module: str, alias_name: str | None = None) -> list[str]:
    candidates: list[str] = []
    if base_module:
        module = base_module
        while module:
            candidates.append(module)
            if "." not in module:
                break
            module = module.rsplit(".", 1)[0]

    if alias_name and base_module and alias_name != "*":
        nested = f"{base_module}.{alias_name}"
        module = nested
        while module:
            candidates.append(module)
            if "." not in module:
                break
            module = module.rsplit(".", 1)[0]

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _find_import_match(
    importer_files: set[str],
    imported_files: set[str],
    imports: dict[str, set[str]],
) -> tuple[str, str] | None:
    for importer_file in sorted(importer_files):
        for imported_file in sorted(imported_files):
            if imported_file in imports.get(importer_file, set()):
                return importer_file, imported_file
    return None


def _has_path(deps: dict[str, set[str]], source: str, target: str) -> bool:
    queue = deque([source])
    visited: set[str] = set()

    while queue:
        current = queue.popleft()
        if current == target:
            return True
        if current in visited:
            continue
        visited.add(current)
        queue.extend(dep for dep in deps.get(current, set()) if dep not in visited)

    return False
