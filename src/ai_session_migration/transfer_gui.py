"""Per-project export, destination preparation, and model selection dialogs."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
import threading

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLineEdit, QMessageBox,
    QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt, QTimer

from .file_restore import build_file_plan, execute_file_restore, plan_rows
from .gui import button, file_restore_space_text, human_size, label
from .handoff_model import CodexModel, CustomModel, GenerationError
from .work_projects import ProjectView, configured_projects, prepare_git, project_files, safe_work_path


GIT_ACTIONS = {"added": "新增", "untracked": "未跟踪", "modified": "修改", "deleted": "删除"}


def git_dependency_note(submodules, lfs_files, checked):
    notes = []
    if submodules:
        present = sum(bool(info.get("localCheckout")) for info in submodules.values())
        notes.append(f"子模块 {len(submodules)} 个，源电脑已检出 {present} 个。目标机需另行获取记录的子模块提交。")
    if lfs_files:
        present = sum(state == "content" for state in lfs_files.values())
        notes.append(f"Git LFS 文件 {len(lfs_files)} 个，源电脑已有内容 {present} 个。Git 恢复后可用仓库权限下载 LFS 内容。")
    if not checked:
        notes.append("子模块和 Git LFS 检查未完成。")
    return "\n".join(notes)


class ExportOptions(QDialog):
    def __init__(self, owner, projects, records, previous=None, draft_count=0, *, model_only=False):
        super().__init__(owner)
        self.setWindowTitle("选择接手稿模型" if model_only else "选择项目和接手稿")
        self.resize(*((720, 380) if model_only else (800, 630)))
        self.projects = projects
        self.model_only = model_only
        self.choices, self.model = {}, None
        self.local_changes = {}
        self.project_sizes = {}
        self.size_cancel = None
        self.size_timer = QTimer(self)
        self.size_timer.setSingleShot(True)
        self.size_timer.setInterval(180)
        self.size_timer.timeout.connect(self.estimate_projects)
        self.finished.connect(lambda _: self.stop_project_estimate())
        layout = QVBoxLayout(self)
        project_area = QWidget(self)
        project_layout = QVBoxLayout(project_area)
        project_layout.setContentsMargins(0, 0, 0, 0)
        project_layout.addWidget(label("项目文件", "Heading"))
        project_layout.addWidget(label("同一个工作树的会话共用这一项。可以带文件、记录 Git 版本，或到新电脑选择已有目录。", "Muted", True))
        scroll = QScrollArea(project_area)
        scroll.setWidgetResizable(True)
        rows = QWidget()
        body = QVBoxLayout(rows)
        for project in projects:
            body.addWidget(label(f"{project.root.name} · {len(project.members)} 条会话 · {', '.join(sorted(project.providers))}", "Section", True))
            root_label = label(str(project.root), "Muted", True)
            body.addWidget(root_label)
            mode = QComboBox()
            mode.addItem("只关联已有目录", "link")
            mode.addItem("随包带工作文件", "files")
            mode.setCurrentIndex(mode.findData("files"))
            if project.remote and project.commit:
                mode.addItem("从 Git 获取指定提交", "git")
            carry = QCheckBox("同时携带本地新增、修改和删除")
            carry.setVisible(False)
            mode.currentIndexChanged.connect(lambda _, m=mode, c=carry: c.setVisible(m.currentData() == "git"))
            if previous and project.id in previous:
                value, keep, *chosen = previous[project.id]
                index = mode.findData(value)
                if index >= 0:
                    mode.setCurrentIndex(index)
                carry.setChecked(bool(keep))
                if isinstance(keep, (tuple, list)):
                    self.local_changes[project.id] = tuple(keep)
                if chosen and chosen[0]:
                    root_label.setText(chosen[0])
            body.addWidget(mode)
            body.addWidget(carry)
            size = label("", "Muted", True)
            body.addWidget(size)
            self.project_sizes[project.id] = size
            if project.commit:
                changes = {state: sum(x == state for x in project.changes.values())
                           for state in GIT_ACTIONS}
                reachability = ("本机跟踪引用包含该提交，到目标机再确认"
                                if project.remote_status == "seen-in-tracking-ref" else "远端可得性待核对，可能尚未推送")
                body.addWidget(label(
                    f"{project.branch} · {project.commit} · " +
                    " · ".join(f"{label} {changes[action]}" for action, label in GIT_ACTIONS.items()) + f"\n{reachability}",
                    "Muted", True))
                dependencies = git_dependency_note(project.submodules, project.lfs_files, project.dependencies_checked)
                if dependencies:
                    body.addWidget(label(dependencies, "Notice", True))
                if project.changes and project.remote:
                    body.addWidget(button("选择要携带的 Git 改动…", lambda _, p=project, c=carry: self.select_changes(p, c)))
            else:
                body.addWidget(button("指定项目根目录…", lambda _, p=project, line=root_label: self.choose_root(p, line)))
            self.choices[project.id] = (mode, carry, root_label, project)
            mode.currentIndexChanged.connect(self.schedule_project_estimate)
            carry.toggled.connect(self.schedule_project_estimate)
        if not projects:
            body.addWidget(label("所选会话没有可扫描的本机项目。已有包内的项目可在导出页选择保留。", "Muted", True))
        body.addStretch()
        scroll.setWidget(rows)
        project_layout.addWidget(scroll, 1)
        self.claude_sessions = [r for r in records if r.provider == "claude" and not r.child]
        self.pages = QStackedWidget()
        self.pages.addWidget(project_area)
        layout.addWidget(self.pages, 1)
        if self.claude_sessions:
            handoff = QWidget()
            handoff_layout = QVBoxLayout(handoff)
            title = "为所选 Claude 会话生成接手稿" if model_only else (
                f"Claude 接手稿 · {len(self.claude_sessions)} 条主会话 · {draft_count} 份可尝试复用")
            handoff_layout.addWidget(label(title, "Section", True))
            self.sessions = QListWidget()
            library = owner.library
            package_readings = (getattr(library, "package_catalog", None) or {}).get("readings", {})
            prompts = library.readings.prompts if hasattr(library, "readings") else {}
            for record in self.claude_sessions:
                if model_only:
                    self.sessions.addItem(record.title)
                    continue
                meta = package_readings.get(record.uid, {})
                prompt = prompts.get(record.session_id)
                has_draft = bool(meta.get("draft") or prompt)
                item = QListWidgetItem(f"{record.title} · {'已有稿件' if has_draft else '待补稿'}")
                if has_draft:
                    item.setData(Qt.ItemDataRole.UserRole, (record.uid, meta, prompt))
                self.sessions.addItem(item)
            handoff_layout.addWidget(self.sessions, 1)
            if not model_only:
                preview = button("预览已有稿件", self.preview_draft)
                preview.setEnabled(False)
                self.sessions.currentItemChanged.connect(
                    lambda item, _: preview.setEnabled(bool(item and item.data(Qt.ItemDataRole.UserRole))))
                handoff_layout.addWidget(preview)
            self.mode = QComboBox()
            if not model_only:
                self.mode.addItem("复用已有稿件，缺稿只带原文", "none")
            suffix = "接手稿" if model_only else "缺稿"
            self.mode.addItem("使用本机 Codex 生成" + suffix, "codex")
            self.mode.addItem("使用自定义模型服务生成" + suffix, "custom")
            handoff_layout.addWidget(self.mode)
            self.custom = QWidget()
            fields = QVBoxLayout(self.custom)
            fields.setContentsMargins(0, 0, 0, 0)
            self.base = QLineEdit()
            self.base.setPlaceholderText("服务地址，例如 https://服务域名/v1")
            self.model_name = QLineEdit()
            self.model_name.setPlaceholderText("模型名")
            self.key = QLineEdit()
            self.key.setEchoMode(QLineEdit.EchoMode.Password)
            self.key.setPlaceholderText("API Key（本次使用）")
            self.protocol = QComboBox()
            self.protocol.addItem("Chat Completions", "chat/completions")
            self.protocol.addItem("Responses", "responses")
            for field in (self.base, self.model_name, self.key, self.protocol):
                fields.addWidget(field)
            handoff_layout.addWidget(self.custom)
            self.custom.hide()
            self.mode.currentIndexChanged.connect(lambda: self.custom.setVisible(self.mode.currentData() == "custom"))
            self.pages.addWidget(handoff)
        actions = QHBoxLayout()
        self.back = button("取消", self.back_or_cancel)
        self.next = button("确认选择", self.next_or_confirm, True)
        actions.addWidget(self.back)
        actions.addStretch()
        actions.addWidget(self.next)
        layout.addLayout(actions)
        if self.claude_sessions and (model_only or not projects):
            self.pages.setCurrentIndex(1)
        self.update_navigation()
        if self.choices:
            self.schedule_project_estimate()

    def stop_project_estimate(self):
        self.size_timer.stop()
        if self.size_cancel is not None:
            self.size_cancel.set()
            self.size_cancel = None

    def schedule_project_estimate(self, *_):
        self.stop_project_estimate()
        needed = False
        for pid, (mode, carry, _, _) in self.choices.items():
            with_files = mode.currentData() == "files" or mode.currentData() == "git" and carry.isChecked()
            self.project_sizes[pid].setText("正在统计要携带的文件…" if with_files else "随包文件 0 个 · 0 B")
            needed = needed or with_files
        if needed:
            self.size_timer.start()

    def estimate_projects(self):
        owner = self.parent()
        cancel = threading.Event()
        self.size_cancel = cancel
        choices = self.values()
        projects = deepcopy(self.projects)
        excluded = [owner.state.root.resolve(), owner.state.cache.resolve()]
        home = owner.library.home

        def check():
            if cancel.is_set():
                raise InterruptedError("已取消项目文件统计")

        def work(_):
            rows = {}
            for project in projects:
                pid = project.id
                mode, carry, _ = choices[pid]
                if mode == "link" or mode == "git" and not carry:
                    continue
                check()
                try:
                    (configured, mode, carry), = configured_projects([project], choices, home)
                    count = total = 0
                    for path, _ in project_files(configured, mode, carry, excluded, check):
                        try:
                            total += path.stat().st_size
                            count += 1
                        except FileNotFoundError:
                            continue
                    rows[pid] = f"随包文件 {count} 个 · 约 {human_size(total)}"
                except (OSError, ValueError) as error:
                    rows[pid] = f"暂时无法统计，导出预览时会重查。{error}"
            return rows

        def done(rows):
            if not cancel.is_set():
                for pid, text in rows.items():
                    self.project_sizes[pid].setText(text)

        owner.task(work, done, "正在统计项目文件", lambda error: done(dict.fromkeys(choices, error)))

    def update_navigation(self):
        project_step = self.pages.currentIndex() == 0
        self.back.setText("返回项目文件" if not project_step and self.projects else "取消")
        self.next.setText("查看 Claude 稿件" if project_step and self.claude_sessions else
                          "检查发送内容" if self.model_only else "选择保存位置")

    def preview_draft(self):
        item = self.sessions.currentItem()
        if not item or not item.data(Qt.ItemDataRole.UserRole):
            return
        uid, meta, prompt = item.data(Qt.ItemDataRole.UserRole)
        try:
            if prompt:
                text = prompt.prompt
            else:
                from .reading_transfer import read_package_draft
                text = read_package_draft(self.parent().library, uid, meta)
        except (OSError, ValueError) as error:
            self.parent().error(str(error))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("已有接手稿")
        dialog.resize(700, 460)
        layout = QVBoxLayout(dialog)
        body = QPlainTextEdit(text)
        body.setReadOnly(True)
        layout.addWidget(body)
        layout.addWidget(button("返回稿件选择", dialog.accept))
        dialog.exec()

    def back_or_cancel(self):
        if self.pages.currentIndex() == 1 and self.projects:
            self.pages.setCurrentIndex(0)
            self.update_navigation()
        else:
            self.reject()

    def next_or_confirm(self):
        if self.pages.currentIndex() == 0 and self.claude_sessions:
            self.pages.setCurrentIndex(1)
            self.update_navigation()
        else:
            self.confirm()

    def confirm(self):
        if self.claude_sessions:
            mode = self.mode.currentData()
            try:
                if mode == "codex":
                    self.model = CodexModel.discover()
                elif mode == "custom":
                    self.model = CustomModel(self.base.text(), self.model_name.text(), self.protocol.currentData(), self.key.text())
                else:
                    self.model = None
            except GenerationError as error:
                self.parent().error(str(error))
                return
            if self.model and not self.review_model_request():
                return
            self.key.clear()
        self.accept()

    def review_model_request(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("确认本批接手稿生成")
        dialog.resize(670, 470)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(self.model.description, "Section", True))
        notice = "向此服务发送下列会话的可读正文和原文位置。"
        if not self.model_only:
            notice += "通过核对的已有成稿将直接复用。"
        layout.addWidget(label(notice, "Info", True))
        sessions = QListWidget()
        for record in self.claude_sessions:
            sessions.addItem(f"{record.title} · {record.session_id}")
        layout.addWidget(sessions, 1)
        actions = QHBoxLayout()
        cancel = button("返回修改", dialog.reject)
        cancel.setDefault(True)
        actions.addWidget(cancel)
        actions.addStretch()
        send = button("确认向所选服务发送", dialog.accept, True)
        send.setAutoDefault(False)
        actions.addWidget(send)
        layout.addLayout(actions)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def values(self):
        return {pid: (mode.currentData(), self.local_changes.get(pid, True) if carry.isChecked() else False,
                      line.text() if line.text() != str(project.root) else "")
                for pid, (mode, carry, line, project) in self.choices.items()}

    def choose_root(self, project, line):
        path = QFileDialog.getExistingDirectory(self, "选择包含当前工作目录的项目根", line.text())
        if path:
            try:
                project.root.relative_to(Path(path).resolve())
            except ValueError:
                self.parent().error("项目根必须包含当前工作目录")
                return
            line.setText(path)
            self.schedule_project_estimate()

    def select_changes(self, project, carry):
        dialog = QDialog(self)
        dialog.setWindowTitle("选择 Git 本地改动")
        dialog.resize(620, 420)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        selected = self.local_changes.get(project.id, project.changes)
        for path, kind in project.changes.items():
            item = QListWidgetItem(GIT_ACTIONS[kind] + " · " + path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setCheckState(Qt.CheckState.Checked if path in selected else Qt.CheckState.Unchecked)
            listing.addItem(item)
        layout.addWidget(listing)
        layout.addWidget(button("确认携带", dialog.accept, True))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            picked = tuple(item.data(Qt.ItemDataRole.UserRole) for index in range(listing.count())
                           if (item := listing.item(index)).checkState() == Qt.CheckState.Checked)
            self.local_changes[project.id] = picked
            carry.setChecked(bool(picked))
            self.schedule_project_estimate()


class ProjectRestore(QDialog):
    def __init__(self, owner, package, *, queue=False):
        super().__init__(owner)
        self.owner, self.package = owner, package
        self.queue = queue
        self.closed = False
        self.finished.connect(self.mark_closed)
        self.setWindowTitle("准备目标机项目")
        self.resize(730, 470)
        layout = QVBoxLayout(self)
        layout.addWidget(label("准备项目", "Heading"))
        layout.addWidget(label("为三个来源共用的项目选择目标目录。项目与会话将在恢复计划中一起核对。"
                               if queue else "为共用项目选择目标目录。准备完成后，会话导入会带入这些路径。", "Muted", True))
        self.projects = QComboBox()
        for project in package.package_catalog.get("projects", []):
            self.projects.addItem(project["title"], project)
        layout.addWidget(self.projects)
        self.mode = QComboBox()
        layout.addWidget(self.mode)
        self.location = QLineEdit()
        self.location.setPlaceholderText("填写目标目录")
        row = QHBoxLayout()
        row.addWidget(self.location, 1)
        row.addWidget(button("选择已有目录", self.choose))
        layout.addLayout(row)
        self.info = label("", "Muted", True)
        layout.addWidget(self.info)
        self.go = button("加入恢复计划" if queue else "继续", self.preview, True)
        layout.addWidget(self.go)
        if queue:
            layout.addWidget(button("从恢复计划移除当前项目", self.remove_choice))
            layout.addWidget(button("返回恢复摘要", self.accept))
        self.projects.currentIndexChanged.connect(self.project_changed)
        self.project_changed()

    def mark_closed(self, _):
        self.closed = True

    def project_changed(self):
        self.mode.clear()
        self.mode.addItem("关联本机已有项目", "link")
        project = self.projects.currentData()
        if not project:
            self.go.setEnabled(False)
            return
        mode = project.get("mode")
        if mode == "files":
            self.mode.addItem("恢复随包工作文件", "files")
            self.mode.setCurrentIndex(1)
        if mode == "git":
            self.mode.addItem("从记录的 Git 地址获取", "git")
            self.mode.setCurrentIndex(1)
        pending = self.owner.pending_projects.get(project["id"]) if self.queue else None
        if pending:
            self.mode.setCurrentIndex(self.mode.findData(pending[0]))
        path = pending[1] if pending else self.owner.project_locations.get(project["id"], "")
        self.location.setText(path)
        git = project.get("git") or {}
        self.info.setText(("已加入恢复计划\n" if pending else "") + f"关联 {len(project.get('members', {}))} 条会话" +
                          (f"\n{git.get('remote', '')}\n提交 {git.get('commit', '')}" if git else ""))

    def choose(self):
        directory = QFileDialog.getExistingDirectory(self, "选目标机项目目录", self.location.text())
        if directory:
            self.location.setText(directory)

    def finish(self, project, path):
        self.owner.project_locations[project["id"]] = str(path)
        self.info.setText("已关联 " + str(path))
        self.setEnabled(True)

    def failed(self, text):
        self.info.setText(text)
        self.setEnabled(True)

    def remove_choice(self):
        pid = self.projects.currentData()["id"]
        self.owner.pending_projects.pop(pid, None)
        self.owner.project_locations.pop(pid, None)
        self.project_changed()

    def preview(self):
        project = self.projects.currentData()
        path = Path(self.location.text().strip()).expanduser()
        if not path.is_absolute():
            self.failed("请填写本机项目的绝对目录")
            return
        mode = self.mode.currentData()
        if mode == "link":
            from .importer import checked_directory
            try:
                path = checked_directory(path)
                if not path.is_dir():
                    raise ValueError("请选择仍存在的项目目录")
            except (OSError, ValueError) as error:
                self.failed(str(error))
                return
            if self.queue:
                self.owner.pending_projects.pop(project["id"], None)
            self.finish(project, path)
            return
        if self.queue:
            self.owner.pending_projects[project["id"]] = (mode, str(path))
            self.info.setText("已加入恢复计划。返回摘要后可选择会话，或仅恢复所选项目。")
            return
        if mode == "git":
            git = project["git"]
            if QMessageBox.question(self, "获取 Git 项目",
                                    f"将在新目录获取 {git['remote']}\n提交 {git['commit']}\n\n{path}") != QMessageBox.StandardButton.Yes:
                return
            self.setEnabled(False)
            self.owner.task(lambda _: prepare_git(project, self.package, path),
                            lambda reviewed: self.confirm_git(project, reviewed), "正在获取指定 Git 提交", self.git_failed)
            return
        self.setEnabled(False)
        try:
            view = ProjectView(self.package, project["id"])
        except (OSError, ValueError) as error:
            self.failed(str(error))
            return
        backups = self.owner.state.root / "项目恢复备份"
        self.owner.task(lambda progress: build_file_plan(view, list(view.items), (), path, backups, progress),
                        lambda plan: self.confirm_files(view, plan, project, path), "预览项目文件冲突", self.failed)

    def git_failed(self, message):
        self.mode.setCurrentIndex(self.mode.findData("link"))
        self.failed(message)

    def confirm_git(self, project, reviewed):
        if self.closed:
            reviewed.close()
            return
        self.setEnabled(True)
        preview = QDialog(self)
        preview.setWindowTitle("核对 Git 项目恢复")
        preview.resize(720, 490)
        layout = QVBoxLayout(preview)
        layout.addWidget(label(f"已核对提交 {reviewed.commit}\n目标 {reviewed.destination}", "Section", True))
        git = project["git"]
        dependencies = git_dependency_note(git.get("submodules", {}), git.get("lfsFiles", {}), git.get("dependenciesChecked"))
        if dependencies:
            layout.addWidget(label(dependencies, "Notice", True))
        entries = QPlainTextEdit()
        entries.setReadOnly(True)
        entries.setPlainText("\n".join(
            f"{GIT_ACTIONS.get(row.action, '未知')} · {row.name}" + (f" · 冲突 {row.conflict}" if row.conflict else "")
            for row in reviewed.changes
        ) or "本包没有选中的本地改动。")
        layout.addWidget(entries, 1)
        actions = QHBoxLayout()
        cancel = button("返回", preview.reject)
        cancel.setDefault(True)
        actions.addWidget(cancel)
        actions.addStretch()
        publish = button("确认恢复到目标目录", preview.accept, True)
        publish.setAutoDefault(False)
        publish.setEnabled(not any(row.conflict for row in reviewed.changes))
        actions.addWidget(publish)
        layout.addLayout(actions)
        if preview.exec() != QDialog.DialogCode.Accepted:
            reviewed.close()
            return

        def apply(_):
            try:
                return reviewed.publish()
            finally:
                reviewed.close()

        self.setEnabled(False)
        self.owner.task(apply, lambda result: self.finish(project, result), "正在确认本地改动并恢复 Git 项目", self.git_failed)

    def confirm_files(self, view, plan, project, path):
        preview = QDialog(self)
        preview.setWindowTitle("核对项目恢复")
        preview.resize(730, 480)
        layout = QVBoxLayout(preview)
        layout.addWidget(label(f"新增 {plan['new']}，跳过 {plan['skip']}，阻止 {plan['blocked']}\n目标备份 {plan['backupRoot']}", "Section", True))
        layout.addWidget(label(file_restore_space_text(plan['space']), "Info", True))
        entries = QPlainTextEdit()
        entries.setReadOnly(True)
        entries.setPlainText("\n".join(f"{row['status']}  {row['path']}  {row['reason']}" for row in plan_rows(plan, limit=1000)))
        layout.addWidget(entries, 1)
        confirm = button("确认备份目标并恢复新增文件", preview.accept, True)
        confirm.setEnabled(plan['space']['enough'])
        layout.addWidget(confirm)
        self.setEnabled(True)
        if preview.exec() != QDialog.DialogCode.Accepted:
            return
        self.setEnabled(False)

        def done(report):
            previous = self.owner.stack.currentWidget()
            self.owner.navigate("recovery")
            self.owner.recovery_center.save_report(report)
            self.owner.stack.setCurrentWidget(previous)
            if report.get("status") == "complete":
                self.finish(project, path)
            else:
                self.failed("项目文件恢复需要检查。请打开操作记录，查看文件回执和目标备份。")
        self.owner.task(lambda progress: execute_file_restore(view, plan, progress), done,
                        "备份目标并恢复项目文件", self.failed)


def mapped_project(record, locations, *, allow_missing=False):
    base = locations.get(record.metadata.get("project_ref"))
    if not base:
        return ""
    relative = record.metadata.get("project_relative", ".")
    if relative != "." and not safe_work_path(relative):
        return ""
    path = Path(base).joinpath(*PurePosixPath(relative).parts)
    return str(path) if allow_missing or path.is_dir() else ""
