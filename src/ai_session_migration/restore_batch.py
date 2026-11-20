"""Review project and native operations together, then run their existing restore engines."""
from dataclasses import dataclass
from pathlib import Path
from collections import Counter

from .file_restore import build_file_plan, execute_file_restore, plan_rows
from .importer import checked_directory
from .safeio import within
from .work_projects import ProjectView, prepare_git


@dataclass
class ReviewedProject:
    project: dict
    path: Path
    view: object = None
    files: dict | None = None
    git: object = None
    attempted: bool = False

    def close(self):
        if self.git:
            self.git.close()
        if self.files and not self.attempted:
            Path(self.files["database"]).unlink(missing_ok=True)

    def planned_directories(self, requested):
        candidates = {p for p in requested if within(p, self.path)}
        if not candidates:
            return set()
        if self.git:
            expected = {p for p in candidates if (self.git.staged / p.relative_to(self.path)).is_dir()}
            writes = (self.path / change.name for change in self.git.changes
                      if not change.conflict and change.action != "deleted")
        else:
            expected = set()
            writes = (Path(row["destination"])
                      for offset in range(0, self.files["selected"], 1000)
                      for row in plan_rows(self.files, offset, 1000) if row["status"] == "new")
        for path in writes:
            expected.update(candidates.intersection(path.parents))
        return expected

    def execute(self, progress, cancel):
        self.attempted = True
        if self.files:
            return execute_file_restore(self.view, self.files, progress, cancel)
        self.git.publish()
        changes = Counter(change.action for change in self.git.changes)
        return {"status": "complete", "target": str(self.path), "commit": self.git.commit,
                "added": changes["added"] + changes["untracked"], "modified": changes["modified"],
                "deleted": changes["deleted"]}


def review_projects(package, choices, protected, backup, progress, cancel):
    projects = {project["id"]: project for project in package.package_catalog.get("projects", [])}
    reviewed = []
    try:
        for pid, (mode, path) in choices.items():
            if cancel.is_set():
                raise InterruptedError("已停止检查恢复计划")
            path = checked_directory(Path(path))
            if any(within(path, other) or within(other, path) for other in protected):
                raise ValueError("项目目录不能与客户端资料、备份位置或其他项目互相包含：" + str(path))
            project = projects[pid]
            progress("检查项目 " + project["title"], 0)
            if mode == "files":
                view = ProjectView(package, pid)
                files = build_file_plan(view, list(view.items), (), path, backup, progress, cancel)
                reviewed.append(ReviewedProject(project, path, view=view, files=files))
            else:
                git = prepare_git(project, package, path)
                reviewed.append(ReviewedProject(project, path, git=git))
            protected = (*protected, path)
        return reviewed
    except Exception:
        for item in reviewed:
            item.close()
        raise
