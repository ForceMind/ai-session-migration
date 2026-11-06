"""Paged, source-preserving Recovery Center; file copies and native imports stay distinct."""
from __future__ import annotations

from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, QRunnable, Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QListWidget, QListWidgetItem, QSplitter, QPlainTextEdit,
    QCheckBox, QFileDialog, QMessageBox, QProgressBar, QTabWidget)

CATEGORIES = {"sessions": "原生会话", "projects": "项目与工作区", "applications": "应用资料 · Mac 文件副本",
    "databases": "数据库文件", "versions": "版本与历史", "snapshots": "快照",
    "keys": "密钥文件 · 仅元数据", "software": "安装与软件", "other": "其他资料"}
STATUSES = {"unindexed": "待建立索引", "ready": "可选择", "key-required": "需要恢复密钥",
    "missing": "缺少文件或卷", "unsupported": "暂不支持", "error": "读取失败",
    "not-captured": "未被备份", "new": "新增", "skip": "跳过", "blocked": "阻止",
    "cancelled": "已取消", "needs-attention": "需要检查", "rolled-back": "已安全回滚",
    "complete": "已完成", "backing-up": "正在备份目标", "restoring": "正在恢复文件"}
KINDS = {"file": "文件", "directory": "文件夹", "symlink": "符号链接描述文件", "notice": "备份说明"}


def status_text(value):
    return STATUSES.get(value, "状态未知")


def verification_text(value):
    return "已通过" if value is True else "未通过" if value is False else "未记录"


def text_label(value, name=None):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def action(value, callback, primary=False):
    widget = QPushButton(value)
    if primary:
        widget.setObjectName("Primary")
    widget.clicked.connect(callback)
    return widget


def size_text(value):
    value = int(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str, object)
    progress = Signal(str, int)


class _Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.last = 0.0

    def progress(self, stage, percent=0):
        now = time.monotonic()
        if now - self.last > .12 or percent >= 100:
            self.last = now
            self.signals.progress.emit(str(stage), max(0, min(100, int(percent or 0))))

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.progress))
        except Exception as exc:
            self.signals.failed.emit(f"{type(exc).__name__}：{exc}", getattr(exc, "report", None))


class RecoveryCenter(QWidget):
    PAGE_SIZE = 200

    def __init__(self, owner, catalog_factory=None):
        super().__init__(owner)
        self.owner = owner
        if catalog_factory is None:
            from .recovery_catalog import RecoveryCatalog
            catalog_factory = RecoveryCatalog
        self.catalog_factory = catalog_factory
        self.catalog = None
        self.loaded_root = None
        self.groups = []
        self.selected_items, self.selected_groups = set(), set()
        self.keys = []
        self.current_group = None
        self.offset, self.total = 0, 0
        self.plan = None
        self.plan_offset = 0
        self.busy = False
        self.cancel = threading.Event()
        self._filling = False
        self._build()
        self.refresh_records()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        heading = QHBoxLayout()
        heading.addWidget(text_label("恢复中心", "Heading"), 1)
        self.cache_button = action("缓存设置…", self.cache_settings)
        heading.addWidget(self.cache_button)
        layout.addLayout(heading)
        layout.addWidget(text_label("浏览所有备份，按文件、文件夹或整组恢复到独立目录；源备份保持不变。", "Subheading"))
        self.cache_note = text_label("", "Muted")
        self.refresh_cache_note()
        layout.addWidget(self.cache_note)
        row = QHBoxLayout()
        self.root_field = QLineEdit()
        self.root_field.setPlaceholderText("选择迁移备份总目录，或其中一个备份目录")
        self.root_field.textEdited.connect(self.invalidate_plan)
        row.addWidget(self.root_field, 1)
        self.root_button = action("选择备份目录", self.choose_root)
        self.load_button = action("读取目录", self.load_root, True)
        self.key_button = action("选择恢复密钥…", self.choose_keys)
        for widget in (self.root_button, self.load_button, self.key_button):
            row.addWidget(widget)
        layout.addLayout(row)
        self.key_info = text_label("密钥只用于解密校验；列表和详情不会显示密钥正文。", "Muted")
        layout.addWidget(self.key_info)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        browse = QWidget()
        browse_layout = QVBoxLayout(browse)
        browse_layout.setContentsMargins(0, 12, 0, 0)
        self.tabs.addTab(browse, "备份与选择")
        split = QSplitter()
        browse_layout.addWidget(split, 1)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        self.category = QComboBox()
        self.category.addItem("全部分类", "")
        for key, value in CATEGORIES.items():
            self.category.addItem(value, key)
        self.category.currentIndexChanged.connect(self.fill_groups)
        ll.addWidget(self.category)
        self.group_list = QListWidget()
        self.group_list.currentItemChanged.connect(self.group_changed)
        self.group_list.itemChanged.connect(self.group_checked)
        ll.addWidget(self.group_list, 1)
        self.index_button = action("索引当前组", self.index_group)
        self.native_button = action("打开原生会话选择…", self.open_native)
        ll.addWidget(self.index_button); ll.addWidget(self.native_button)
        split.addWidget(left)
        middle = QWidget(); ml = QVBoxLayout(middle); ml.setContentsMargins(0, 0, 0, 0)
        sr = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("搜索当前组的路径")
        self.search.returnPressed.connect(self.search_items)
        self.search_button = action("搜索", self.search_items)
        sr.addWidget(self.search, 1); sr.addWidget(self.search_button); ml.addLayout(sr)
        self.items = QListWidget()
        self.items.itemChanged.connect(self.item_checked)
        self.items.currentItemChanged.connect(self.item_detail)
        ml.addWidget(self.items, 1)
        pager = QHBoxLayout()
        self.previous = action("上一页", lambda: self.move_page(-1))
        self.next = action("下一页", lambda: self.move_page(1))
        self.page_info = text_label("尚未读取", "Muted")
        pager.addWidget(self.previous); pager.addWidget(self.page_info, 1); pager.addWidget(self.next)
        ml.addLayout(pager)
        self.page_select = action("勾选本页可恢复项", lambda: self.select_page(True))
        self.clear_selection = action("清空选择", self.clear_selected)
        picks = QHBoxLayout(); picks.addWidget(self.page_select); picks.addWidget(self.clear_selection)
        ml.addLayout(picks)
        self.selection_info = text_label("已选 0 项 · 0 整组", "Muted"); ml.addWidget(self.selection_info)
        split.addWidget(middle)
        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(text_label("元数据与范围", "Section"))
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setPlaceholderText("选择一项查看路径、大小、校验值和备份状态。密钥正文不预览。")
        rl.addWidget(self.details, 1)
        rl.addWidget(text_label("Mac 应用资料在此仅恢复文件副本。登录、Windows 原生可用性及模型续聊仍需目标机验证。", "Notice"))
        split.addWidget(right); split.setSizes([250, 500, 290])
        target_row = QHBoxLayout()
        self.target = QLineEdit(); self.target.setPlaceholderText("文件副本目标目录（独立于原客户端）")
        self.target_button = action("目标位置…", lambda: self.choose_directory(self.target, "选择独立恢复目标目录"))
        self.backup = QLineEdit(); self.backup.setPlaceholderText("完整目标备份和恢复记录的保存目录")
        self.backup_button = action("目标备份位置…", lambda: self.choose_directory(self.backup, "选择目标备份与记录目录"))
        for widget in (self.target, self.target_button, self.backup, self.backup_button):
            target_row.addWidget(widget)
        browse_layout.addLayout(target_row)
        self.preview_button = action("预览文件恢复计划", self.preview, True)
        browse_layout.addWidget(self.preview_button)
        for field in (self.target, self.backup):
            field.textChanged.connect(self.invalidate_plan)
        preview = QWidget(); pl = QVBoxLayout(preview); pl.setContentsMargins(0, 12, 0, 0)
        self.tabs.addTab(preview, "冲突预览与执行")
        self.summary = text_label("先选择资料和目标位置，再生成计划。", "Info"); pl.addWidget(self.summary)
        self.plan_list = QListWidget(); pl.addWidget(self.plan_list, 1)
        preview_pager = QHBoxLayout()
        self.plan_previous = action("上一页", lambda: self.move_plan(-1))
        self.plan_next = action("下一页", lambda: self.move_plan(1))
        self.plan_page_info = text_label("", "Muted")
        preview_pager.addWidget(self.plan_previous); preview_pager.addWidget(self.plan_page_info, 1); preview_pager.addWidget(self.plan_next)
        pl.addLayout(preview_pager)
        self.ack = QCheckBox("我知悉：将先完整备份现有目标；该备份含明文私人资料，已选择受控保存位置。")
        self.ack.toggled.connect(self.update_controls); pl.addWidget(self.ack)
        self.restore_button = action("备份目标并恢复文件副本", self.restore, True); pl.addWidget(self.restore_button)
        records = QWidget(); rec = QVBoxLayout(records); rec.setContentsMargins(0, 12, 0, 0)
        self.tabs.addTab(records, "结果与安全回滚")
        self.record_list = QListWidget(); self.record_list.currentItemChanged.connect(self.record_detail)
        rec.addWidget(self.record_list, 1)
        self.record_details = QPlainTextEdit(); self.record_details.setReadOnly(True); rec.addWidget(self.record_details, 1)
        record_actions = QHBoxLayout()
        self.check_record_button = action("检查所选记录", self.check_record)
        self.open_record_button = action("打开记录目录", self.open_record)
        self.rollback_button = action("安全回滚所选记录…", self.rollback)
        for widget in (self.check_record_button, self.open_record_button, self.rollback_button):
            record_actions.addWidget(widget)
        rec.addLayout(record_actions)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(1000)
        self.log.setMaximumHeight(105); rec.addWidget(self.log)
        progress_row = QHBoxLayout()
        self.stage = text_label("选择备份目录开始；可随时查看本地恢复记录。", "Muted")
        self.progress = QProgressBar(); self.progress.setMaximumWidth(180)
        self.cancel_button = action("取消当前任务", self.cancel_task)
        progress_row.addWidget(self.stage, 1); progress_row.addWidget(self.progress); progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)
        self.update_controls()

    def run_task(self, fn, done, stage, cancellable=True):
        if self.busy or self.owner.busy or self.owner.loading:
            self.show_error("已有任务正在运行，请等待完成或取消当前任务。")
            return
        self.busy = self.owner.busy = True
        self.cancel = threading.Event(); self.owner.cancel = self.cancel
        self.progress.setValue(0); self.stage.setText(stage)
        self.update_controls()
        self.cancel_button.setEnabled(cancellable)
        task = _Task(fn); self.owner.tasks.add(task)
        def progress(text, percent):
            self.stage.setText(text); self.progress.setValue(percent)
        def finish(value):
            self.owner.tasks.discard(task)
            self.busy = self.owner.busy = False
            self.stage.setText("任务结束" if not self.cancel.is_set() else "任务已结束 · 已请求取消")
            try:
                done(value)
            except Exception as exc:
                self.show_error(f"无法展示结果：{type(exc).__name__}：{exc}")
            self.update_controls()
        def failure(message, report):
            self.owner.tasks.discard(task)
            self.busy = self.owner.busy = False
            if report:
                self.save_report(report)
            self.show_error(message)
            if self.catalog:
                self.groups = self.catalog.groups()
                self.fill_groups()
            self.update_controls()
        task.signals.progress.connect(progress)
        task.signals.done.connect(finish); task.signals.failed.connect(failure)
        self.owner.pool.start(task)

    def show_error(self, message):
        self.stage.setText(message)
        self.log.appendPlainText(message)

    def cancel_task(self):
        self.cancel.set()
        self.stage.setText("已请求取消，正在完成当前安全边界；请等待结果记录。")
        self.cancel_button.setEnabled(False)

    def update_controls(self, *_):
        idle = not self.busy
        self.cache_button.setEnabled(idle and not self.owner.busy and not self.owner.loading)
        self.cancel_button.setEnabled(self.busy and not self.cancel.is_set())
        for widget in (self.root_field, self.root_button, self.load_button, self.key_button, self.category,
                       self.group_list, self.items, self.search, self.search_button, self.page_select,
                       self.clear_selection, self.target, self.target_button, self.backup, self.backup_button):
            widget.setEnabled(idle)
        current = self.current_group or {}
        self.index_button.setEnabled(idle and bool(current) and self.source_matches())
        self.native_button.setEnabled(idle and bool(current.get("native_archive")))
        self.previous.setEnabled(idle and self.offset > 0)
        self.next.setEnabled(idle and self.offset + self.PAGE_SIZE < self.total)
        self.preview_button.setEnabled(idle and bool(self.catalog) and self.source_matches() and bool(self.selected_items or self.selected_groups))
        self.ack.setEnabled(idle and bool(self.plan))
        self.restore_button.setEnabled(idle and bool(self.plan) and self.ack.isChecked() and bool(self.plan.get("new")))
        self.plan_previous.setEnabled(idle and bool(self.plan) and self.plan_offset > 0)
        self.plan_next.setEnabled(idle and bool(self.plan) and self.plan_offset + self.PAGE_SIZE < int((self.plan or {}).get("selected", 0)))
        for widget in (self.check_record_button, self.open_record_button, self.rollback_button):
            widget.setEnabled(idle and self.record_list.currentItem() is not None)

    def refresh_cache_note(self):
        self.cache_note.setText(f"大型备份的目录索引使用缓存；内置磁盘空间不足时，可在缓存设置中选择移动磁盘。当前：{self.owner.state.cache}")

    def cache_settings(self):
        if self.busy or self.owner.busy or self.owner.loading:
            self.show_error("请先等待当前任务结束，再更改缓存位置。")
            return
        previous = self.owner.state.cache
        self.owner.show_settings()
        self.refresh_cache_note()
        if self.owner.state.cache != previous:
            self.loaded_root = None
            self.invalidate_plan()
            # Re-open the selected source using the newly chosen cache, before indexing.
            if self.root_field.text().strip():
                self.load_root()

    def source_matches(self):
        return bool(self.loaded_root and Path(self.root_field.text()).expanduser().resolve() == self.loaded_root)

    def choose_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择迁移备份目录", self.root_field.text())
        if path:
            self.root_field.setText(path); self.load_root()

    def load_root(self):
        if not self.root_field.text().strip():
            self.show_error("请先选择备份目录。")
            return
        root = Path(self.root_field.text()).expanduser()
        if not root.is_dir():
            self.show_error("备份目录不可访问，请连接对应磁盘或选择现有目录。")
            return
        def work(progress):
            catalog = self.catalog_factory(root, self.owner.state.cache / "recovery", keys=self.keys)
            try:
                groups = catalog.discover()
                return catalog, groups
            except Exception:
                catalog.close()
                raise
        def done(value):
            old = self.catalog
            self.catalog, self.groups = value
            self.loaded_root = root.resolve()
            if old:
                old.close()
            self.clear_selected(); self.current_group = None
            self.items.clear(); self.total = self.offset = 0
            self.fill_groups()
            self.stage.setText(f"已识别 {len(self.groups)} 组备份" if self.groups else "目录内没有可识别备份；源文件保持不变。")
        self.run_task(work, done, "正在发现备份分类")

    def choose_keys(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择恢复密钥（只读取，不显示正文）", "", "恢复密钥 (*.asmkey *.key);;所有文件 (*)")
        if paths:
            self.keys = [Path(path) for path in paths]
            self.key_info.setText(f"已选择 {len(self.keys)} 个密钥文件 · 仅用于解密认证 · 正文不预览")
            if self.root_field.text().strip():
                self.load_root()

    def fill_groups(self, *_):
        selected = (self.current_group or {}).get("id")
        self._filling = True; self.group_list.clear()
        for group in self.groups:
            if self.category.currentData() and group.get("category") != self.category.currentData():
                continue
            item = QListWidgetItem(f"{group.get('label', '')}\n{CATEGORIES.get(group.get('category'), '其他')} · {STATUSES.get(group.get('status'), group.get('status', ''))}\n{group.get('count', 0)} 项 · {size_text(group.get('size', 0))}")
            item.setData(Qt.ItemDataRole.UserRole, group)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if group["id"] in self.selected_groups else Qt.CheckState.Unchecked)
            if group.get("status") in {"missing", "unsupported", "error"}:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.group_list.addItem(item)
            if group["id"] == selected:
                self.group_list.setCurrentItem(item)
        self._filling = False
        self.update_controls()

    def group_changed(self, item, previous=None):
        self.current_group = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.offset = self.total = 0; self.items.clear()
        group = self.current_group or {}
        self.details.setPlainText("\n".join([group.get("label", ""), CATEGORIES.get(group.get("category"), ""),
            f"状态：{STATUSES.get(group.get('status'), group.get('status', ''))}", str(group.get("path", "")), str(group.get("detail", ""))]))
        if not self._filling and group.get("status") == "ready":
            self.load_items()
        self.update_controls()

    def group_checked(self, item):
        if self._filling:
            return
        group = item.data(Qt.ItemDataRole.UserRole)
        (self.selected_groups.add if item.checkState() == Qt.CheckState.Checked else self.selected_groups.discard)(group["id"])
        self.selection_changed()

    def index_group(self):
        if not self.source_matches():
            self.show_error("备份目录或缓存已变化，请先重新读取目录。")
            return
        group = self.current_group
        if not group:
            return
        def done(value):
            self.groups = self.catalog.groups()
            self.current_group = value
            self.fill_groups()
            if value.get("status") == "ready":
                self.load_items()
            else:
                self.show_error(STATUSES.get(value.get("status"), "索引未完成") + "：" + str(value.get("detail", "")))
        self.run_task(lambda progress: self.catalog.index_group(group["id"], progress=progress, cancel=self.cancel), done, "正在校验和索引当前组")

    def search_items(self):
        self.offset = 0; self.load_items()

    def move_page(self, direction):
        self.offset = max(0, self.offset + direction * self.PAGE_SIZE); self.load_items()

    def load_items(self):
        group = self.current_group
        if not group or group.get("status") != "ready":
            return
        offset, search = self.offset, self.search.text().strip()
        def work(progress):
            return self.catalog.count_items(group["id"], search=search), self.catalog.list_items(group["id"], search=search, offset=offset, limit=self.PAGE_SIZE)
        def done(value):
            self.total, rows = value
            self._filling = True; self.items.clear()
            for row in rows:
                item = QListWidgetItem(f"{row.get('path', '')}\n{KINDS.get(row.get('kind'), '未知类型')} · {size_text(row.get('size'))} · {STATUSES.get(row.get('status'), row.get('status', ''))}")
                item.setData(Qt.ItemDataRole.UserRole, row)
                if row.get("status") == "ready" and row.get("kind") != "notice":
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected_items else Qt.CheckState.Unchecked)
                self.items.addItem(item)
            self._filling = False
            self.page_info.setText(f"{offset + 1 if rows else 0}–{offset + len(rows)} / {self.total} 项")
            self.stage.setText("当前组没有匹配项目。" if not rows else "目录选择包含该文件夹内项目；整组选项覆盖所有分页。")
        self.run_task(work, done, "正在读取索引分页")

    def item_detail(self, item, previous=None):
        if not item:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        # Deliberately show only known metadata fields; internal locators and secrets stay hidden.
        fields = [("恢复相对路径", row.get("path")), ("原始路径（仅显示）", row.get("original_path")),
            ("类型", KINDS.get(row.get("kind"), "未知类型")), ("大小", size_text(row.get("size"))),
            ("状态", status_text(row.get("status"))), ("SHA-256", row.get("sha256")), ("说明", row.get("detail"))]
        self.details.setPlainText("\n".join(f"{name}：{value}" for name, value in fields if value is not None and value != ""))

    def item_checked(self, item):
        if self._filling:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        (self.selected_items.add if item.checkState() == Qt.CheckState.Checked else self.selected_items.discard)(row["id"])
        self.selection_changed()

    def select_page(self, checked):
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def clear_selected(self):
        self.selected_items.clear(); self.selected_groups.clear()
        self._filling = True
        for listing in (self.group_list, self.items):
            for index in range(listing.count()):
                item = listing.item(index)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Unchecked)
        self._filling = False; self.selection_changed()

    def selection_changed(self):
        self.selection_info.setText(f"已选 {len(self.selected_items)} 个文件/文件夹 · {len(self.selected_groups)} 整组（重复项自动去重）")
        self.invalidate_plan()

    def invalidate_plan(self, *_):
        self.plan = None; self.ack.setChecked(False); self.plan_list.clear()
        self.summary.setText("选择或目标已变化，请重新生成预览。")
        self.update_controls()

    def choose_directory(self, field, title):
        path = QFileDialog.getExistingDirectory(self, title, field.text())
        if path:
            field.setText(path)

    def preview(self):
        if not self.source_matches():
            self.show_error("备份目录已变化，请先点击读取目录。")
            return
        if not self.catalog or not (self.selected_items or self.selected_groups):
            self.show_error("请勾选文件、文件夹或整组。")
            return
        if not self.target.text().strip() or not self.backup.text().strip():
            self.show_error("请选择独立恢复目标和目标备份位置。")
            return
        from .file_restore import build_file_plan
        items, groups = tuple(self.selected_items), tuple(self.selected_groups)
        target, backup = Path(self.target.text()).expanduser(), Path(self.backup.text()).expanduser()
        def work(progress):
            by_id = {group["id"]: group for group in self.catalog.groups()}
            for group_id in groups:
                if self.cancel.is_set():
                    raise InterruptedError("已取消预览")
                if by_id[group_id].get("status") != "ready":
                    group = self.catalog.index_group(group_id, progress=progress, cancel=self.cancel)
                    if group.get("status") != "ready":
                        raise ValueError(f"{group.get('label', '')}：{STATUSES.get(group.get('status'), group.get('status'))}；{group.get('detail', '')}")
            return build_file_plan(self.catalog, items, groups, target, backup, progress=progress, cancel=self.cancel)
        def done(plan):
            self.plan = plan; self.plan_offset = 0; self.ack.setChecked(False)
            self.groups = self.catalog.groups(); self.fill_groups()
            self.summary.setText(f"目标：{plan['target']}\n目标备份：{plan['backupRoot']}\n新增 {plan['new']} · 跳过 {plan['skip']} · 阻止 {plan['blocked']} · {size_text(plan.get('bytes'))}\n已有文件不覆盖；阻止项不会写入。Mac 应用资料仅导出副本。")
            self.tabs.setCurrentIndex(1); self.load_plan_page()
        self.run_task(work, done, "正在生成冲突与路径安全预览")

    def move_plan(self, direction):
        self.plan_offset = max(0, self.plan_offset + direction * self.PAGE_SIZE); self.load_plan_page()

    def load_plan_page(self):
        if not self.plan:
            return
        from .file_restore import plan_rows
        plan, offset = self.plan, self.plan_offset
        def done(rows):
            self.plan_list.clear()
            for row in rows:
                item = QListWidgetItem(f"{STATUSES.get(row.get('status'), row.get('status', ''))} · {row.get('path', '')}\n{row.get('reason', '')}\n{row.get('destination', '')}")
                self.plan_list.addItem(item)
            self.plan_page_info.setText(f"{offset + 1 if rows else 0}–{offset + len(rows)} / {plan.get('selected', 0)} 项")
        self.run_task(lambda progress: plan_rows(plan, offset=offset, limit=self.PAGE_SIZE), done, "正在读取计划分页")

    def restore(self):
        if not self.source_matches():
            self.show_error("备份目录已变化，请重新读取并生成预览。")
            return
        if not self.plan or not self.ack.isChecked() or not self.plan.get("new"):
            self.show_error("请先生成预览并确认明文目标备份的隐私提示。")
            return
        from .file_restore import execute_file_restore
        plan = self.plan
        self.ack.setChecked(False)
        # A completed/partial plan must be rebuilt against the target before any retry.
        self.plan = None
        self.run_task(lambda progress: execute_file_restore(self.catalog, plan, progress=progress, cancel=self.cancel), self.save_report, "正在备份目标并恢复文件副本")

    def save_report(self, report):
        records = self.owner.state.data.setdefault("fileRestores", [])
        records[:] = [row for row in records if row.get("journal") != report.get("journal")]
        records.insert(0, report)
        self.owner.state.save()
        self.plan = None; self.ack.setChecked(False)
        self.refresh_records(); self.tabs.setCurrentIndex(2)
        self.log.appendPlainText(f"状态：{status_text(report.get('status'))}；已恢复 {report.get('restored', 0)}；跳过 {report.get('skipped', 0)}；阻止 {report.get('blocked', 0)}")
        self.stage.setText("恢复记录已保存 · 请核对目标备份、回读验证和结果状态")

    def refresh_records(self):
        self.record_list.clear()
        for report in self.owner.state.data.get("fileRestores", []):
            item = QListWidgetItem(f"{status_text(report.get('status'))} · 已恢复 {report.get('restored', 0)} 项\n{report.get('target', report.get('output', ''))}")
            item.setData(Qt.ItemDataRole.UserRole, report); self.record_list.addItem(item)
        if self.record_list.count():
            self.record_list.setCurrentRow(0)
        self.update_controls()

    def record_detail(self, item, previous=None):
        if item:
            report = item.data(Qt.ItemDataRole.UserRole)
            recheck = verification_text(report.get("backupRecheckVerified")) if "backupRecheckVerified" in report else "尚未复核"
            lines = [f"状态：{status_text(report.get('status'))}",
                f"恢复目标：{report.get('target') or '未记录'}",
                f"完整目标备份：{report.get('backup') or '尚未生成'}",
                f"初次备份校验：{verification_text(report.get('backupVerified'))}",
                f"最近备份复核：{recheck}",
                f"恢复内容回读：{verification_text(report.get('readbackVerified'))}",
                f"已恢复 {report.get('restored', 0)} 项 · 跳过 {report.get('skipped', 0)} 项 · 阻止 {report.get('blocked', 0)} 项"]
            if "pendingFiles" in report:
                lines.append(f"最近内容检查：已核验 {report.get('checkedPublishedFiles', 0)} 项 · 异常或待完成 {report['pendingFiles']} 项")
            if "rollbackVerified" in report:
                lines.append(f"安全回滚核验：{verification_text(report.get('rollbackVerified'))} · 已移除 {report.get('rollbackRemovedFiles', 0)} 个未变文件")
            protected = report.get("protectedUserChanges") or []
            if protected:
                lines.append(f"用户后续修改：已保护 {len(protected)} 个文件，其修改内容保留。")
                lines.extend(str(path) for path in protected[:5])
                if len(protected) > 5:
                    lines.append("其余保护路径请查看详细记录。")
            else:
                lines.append("回滚保护：只移除本次创建且未被修改的文件；用户后续修改会保留。")
            if report.get("nativeClientRestored") is False:
                lines.append("原生客户端恢复：未执行；本次仅恢复独立文件副本。")
            else:
                lines.append("原生客户端恢复：请以原生会话导入记录为准。")
            if report.get("error"):
                lines.append("需要关注：" + str(report['error']))
            lines.append("点击“打开记录目录”查看完整日志和逐项记录。")
            self.record_details.setPlainText("\n".join(lines))
        else:
            self.record_details.clear()
        self.update_controls()

    def selected_record(self):
        item = self.record_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def check_record(self):
        report = self.selected_record()
        if not report:
            return
        from .file_restore import check_file_restore
        def done(value):
            self.save_report(value)
            self.log.appendPlainText("记录检查完成；请查看备份、目标文件和中断状态的核验结果。")
        self.run_task(lambda progress: check_file_restore(report, progress=progress), done, "正在核验恢复记录、目标文件与备份", cancellable=False)

    def open_record(self):
        report = self.selected_record()
        if report and report.get("journal"):
            path = Path(report["journal"]).parent
            if path.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                self.show_error("记录目录不可访问，请连接对应磁盘。")

    def rollback(self):
        report = self.selected_record()
        if not report:
            return
        answer = QMessageBox.question(self, "安全回滚文件副本", "只移除本次创建且内容未变的文件和空目录；之后修改过的文件会保留。\n\n确认检查并回滚这条记录？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from .file_restore import rollback_file_restore
        self.run_task(lambda progress: rollback_file_restore(report, progress=progress), self.save_report, "正在核验日志并安全回滚", cancellable=False)

    def open_native(self):
        group = self.current_group or {}
        path = group.get("native_archive")
        if not path:
            self.show_error("本组没有原生会话迁移包；可使用文件副本恢复。")
            return
        if not Path(path).is_file():
            self.show_error("原生会话包不可访问，请连接对应磁盘。")
            return
        self.owner.open_package_path(path)
