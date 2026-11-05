from __future__ import annotations

import base64
import html
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Qt, QSize, QUrl, QTimer, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QListWidget, QListWidgetItem, QSplitter,
    QStackedWidget, QScrollArea, QTextBrowser, QPlainTextEdit, QTabWidget, QCheckBox,
    QDialog, QFileDialog, QMessageBox, QProgressBar, QToolButton, QInputDialog,
    QStyledItemDelegate, QStyle, QAbstractItemView, QButtonGroup)

from . import __version__
from .adapters import LocalLibrary
from .backup import make_backup, save_recovery_key, verify_package, verify_encrypted, decrypt_for_preview, resume_backup
from .models import PROVIDERS, Attachment
from .packages import PackageLibrary
from .importer import build_plan, execute_import, rollback_import, validate_backup_location
from .safeio import within
from .state import AppState
from .theme import STYLE


def human_size(n):
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PiB"


def label(text, name=None, wrap=False):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    w.setWordWrap(wrap)
    if name:
        w.setObjectName(name)
    return w


def button(text, fn, primary=False, quiet=False):
    b = QPushButton(text)
    b.setObjectName("Primary" if primary else "Quiet" if quiet else "")
    b.clicked.connect(fn)
    return b


def card():
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(21, 19, 21, 19)
    lay.setSpacing(12)
    return f, lay


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


class TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str, object)
    progress = Signal(str, int)


class Task(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()
        self._last = ("", -1, 0.0)

    def emit(self, stage, percent=0):
        old, value, when = self._last
        if stage != old or (percent != value and time.monotonic() - when > .1) or percent >= 100:
            self._last = (stage, percent, time.monotonic())
            self.signals.progress.emit(stage, percent)

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.emit))
        except Exception as error:
            self.signals.failed.emit(type(error).__name__ + "：" + str(error), getattr(error, "report", None))


class SafeText(QTextBrowser):
    def __init__(self, text="", plain=False):
        super().__init__()
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if plain:
            self.setPlainText(text)
        else:
            self.setMarkdown(text)
        self.setMinimumHeight(48)
        self.setMaximumHeight(470)
        self.document().setDocumentMargin(1)
        self.document().documentLayout().documentSizeChanged.connect(self.fit_height)
        QTimer.singleShot(0, self.fit_height)

    def loadResource(self, resource_type, url):
        # Historical Markdown must not load network URLs or arbitrary local files.
        return None

    def fit_height(self, *_):
        h = int(self.document().size().height()) + 15
        self.setFixedHeight(min(470, max(48, h)))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if h > 470 else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.document().setTextWidth(max(100, self.viewport().width() - 4))
        self.fit_height()


class SessionDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        r = index.data(Qt.ItemDataRole.UserRole)
        if r is None:
            return super().paint(painter, option, index)
        painter.save()
        box = option.rect
        chosen = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(box, QColor("#25382f" if chosen else "#191c20"))
        if chosen:
            painter.fillRect(box.x(), box.y(), 3, box.height(), QColor("#b8e7d4"))
        painter.setPen(QPen(QColor("#2b3139")))
        painter.drawLine(box.bottomLeft(), box.bottomRight())
        x, y = box.x() + 14, box.y() + 17
        checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
        painter.setPen(QColor("#b8e7d4" if checked else "#5c6a7a"))
        painter.setBrush(QColor("#b8e7d4") if checked else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(x, y + 1, 13, 13, 3, 3)
        if checked:
            painter.setPen(QPen(QColor("#183426"), 1.5))
            painter.drawLine(x + 3, y + 7, x + 6, y + 10)
            painter.drawLine(x + 6, y + 10, x + 11, y + 4)
        font = painter.font()
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor("#e1e6ee"))
        title = option.fontMetrics.elidedText(r.title, Qt.TextElideMode.ElideRight, box.width() - 67)
        painter.drawText(x + 25, y + 13, title)
        colors = {"claude": ("#4b372c", "#e1bda3", "C"), "codex": ("#34403e", "#d2e9db", "↗"), "deepseek": ("#293b61", "#a2bdff", "D")}
        bg, fg, char = colors[r.provider]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(x + 25, y + 26, 14, 14, 4, 4)
        font.setPixelSize(10)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(fg))
        painter.drawText(x + 28, y + 37, char)
        painter.setPen(QColor("#8c9aab"))
        meta = r.project + (" · 已归档" if r.archived else "") + (" · 子任务" if r.child else "")
        painter.drawText(x + 46, y + 37, option.fontMetrics.elidedText(meta, Qt.TextElideMode.ElideRight, box.width() - 120))
        if r.issue:
            painter.setPen(QColor("#e9c17d"))
            painter.drawText(x + 25, y + 57, r.issue[:27])
        painter.restore()

    def sizeHint(self, option, index):
        r = index.data(Qt.ItemDataRole.UserRole)
        return QSize(310, 91 if r and r.issue else 76)


class MainWindow(QMainWindow):
    def __init__(self, home=None, state_root=None):
        super().__init__()
        self.setWindowTitle("AI Session Migration")
        self.resize(1400, 900)
        self.setMinimumSize(1080, 720)
        self.state = AppState(state_root)
        self.home = Path(home or Path.home())
        self.library = LocalLibrary(self.home, self.state.cache, self.state.data.get("sources", []))
        self.catalog = None
        self.records = []
        self.record_map = {}
        self.current_record = None
        self.selected = set(self.state.data.get("selection", []))
        self.marks = set(self.state.data.get("marks", []))
        self.source_filter, self.project_filter = "all", "all"
        self.limit = 80
        self.detail_generation = 0
        self.busy = False
        self.cancel = threading.Event()
        self.tasks = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._building_list = False
        self.detail_cancel = threading.Event()
        self.loading = False
        self._closing = False
        self._build()
        self.show_welcome()

    def _build(self):
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(208)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 26, 12, 17)
        side.setSpacing(5)
        side.addWidget(label("↗  会话迁移", "Brand"))
        side.addWidget(label("AI Session Migration", "Muted"))
        side.addSpacing(22)
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = {}
        for name, route in [("▤  会话库", "library"), ("▣  备份库", "backups"), ("⇄  迁移清单", "transfer"), ("↙  导入记录", "imports")]:
            b = button(name, lambda _=False, r=route: self.navigate(r))
            b.setObjectName("Nav")
            b.setCheckable(True)
            self.nav_group.addButton(b)
            self.nav_buttons[route] = b
            side.addWidget(b)
        side.addWidget(label("来源", "SidebarCaption"))
        self.provider_buttons = {}
        for key, name in PROVIDERS.items():
            b = button(name, lambda _=False, p=key: self.filter_provider(p))
            b.setObjectName("Nav")
            self.provider_buttons[key] = b
            side.addWidget(b)
        side.addWidget(label("项目", "SidebarCaption"))
        self.projects = QListWidget()
        self.projects.setStyleSheet("QListWidget{background:transparent;border:0;font-size:12px} QListWidget::item{padding:8px 10px;border:0}")
        self.projects.itemClicked.connect(self.filter_project)
        side.addWidget(self.projects, 1)
        side.addWidget(button("⊞  来源管理", lambda: self.navigate("sources"), quiet=True))
        side.addWidget(button("⚙  设置", self.show_settings, quiet=True))
        side.addWidget(label("● 本地优先 · 开源", "Muted"))
        outer.addWidget(sidebar)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.pages = {}
        self._build_library()
        self.status = QLabel("本地资料 · 不更改源会话")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(label("Windows 导入与续聊 · 待目标机验证", "Muted"))
        menu = self.menuBar().addMenu("文件")
        for title, fn in [("识别本机资料", self.scan), ("添加资料目录…", self.add_source), ("打开迁移包预览…", self.open_package), ("导入到本机…", self.new_import), ("创建备份…", self.new_backup), ("退出", self.close)]:
            action = QAction(title, self)
            action.triggered.connect(fn)
            menu.addAction(action)
        help_menu = self.menuBar().addMenu("帮助")
        about = QAction("关于 AI Session Migration", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def _page(self, key):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(17)
        self.pages[key] = page
        self.stack.addWidget(page)
        return page, layout

    def _heading(self, layout, title, subtitle, buttons=()):
        row = QHBoxLayout()
        words = QVBoxLayout()
        words.addWidget(label(title, "Heading"))
        words.addWidget(label(subtitle, "Subheading"))
        row.addLayout(words, 1)
        for b in buttons:
            row.addWidget(b)
        layout.addLayout(row)

    def _build_library(self):
        page, layout = self._page("library")
        self._heading(layout, "会话库", "所有本地记录，在一个地方整理", [button("打开迁移包", self.open_package), button("导入到本机", self.new_import), button("新建备份", self.new_backup, True)])
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索会话标题、项目或来源")
        self.search.textChanged.connect(self.populate_sessions)
        bar.addWidget(self.search, 1)
        self.filter_box = QComboBox()
        self.filter_box.addItems(["全部状态", "已归档", "需要关注", "已标记"])
        self.filter_box.currentIndexChanged.connect(self.populate_sessions)
        bar.addWidget(self.filter_box)
        self.show_children = QCheckBox("包括子任务")
        self.show_children.toggled.connect(self.populate_sessions)
        bar.addWidget(self.show_children)
        bar.addWidget(button("刷新", self.scan))
        layout.addLayout(bar)
        split = QSplitter()
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(8)
        head = QHBoxLayout()
        self.select_all = QCheckBox("选择当前列表")
        self.select_all.clicked.connect(self.select_visible)
        head.addWidget(self.select_all)
        self.list_count = label("尚未读取", "Muted")
        head.addWidget(self.list_count, 1, Qt.AlignmentFlag.AlignRight)
        left_lay.addLayout(head)
        self.session_list = QListWidget()
        self.session_list.setItemDelegate(SessionDelegate(self.session_list))
        self.session_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.session_list.currentItemChanged.connect(self.choose_session)
        self.session_list.itemChanged.connect(self.selection_changed)
        left_lay.addWidget(self.session_list, 1)
        self.selection_label = label("未选择会话", "Muted")
        left_lay.addWidget(self.selection_label)
        split.addWidget(left)
        self.detail_frame, d = card()
        top = QHBoxLayout()
        self.detail_title = label("选择一条会话", "Section", True)
        top.addWidget(self.detail_title, 1)
        self.star = button("☆", self.toggle_mark, quiet=True)
        top.addWidget(self.star)
        top.addWidget(button("加入迁移清单", self.add_current, quiet=True))
        d.addLayout(top)
        self.detail_subtitle = label("正文、工具记录和现有 thinking 只读展示", "Muted", True)
        d.addWidget(self.detail_subtitle)
        self.copy_selector = QComboBox()
        self.copy_selector.setToolTip("只切换当前预览的日志副本，不合并或改写原始文件")
        self.copy_selector.currentIndexChanged.connect(self.change_copy)
        self.copy_selector.hide()
        d.addWidget(self.copy_selector)
        self.detail_notice = label("", "Notice", True)
        self.detail_notice.hide()
        d.addWidget(self.detail_notice)
        self.tabs = QTabWidget()
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_widget = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_widget)
        self.chat_layout.setContentsMargins(1, 14, 7, 5)
        self.chat_layout.setSpacing(17)
        self.chat_scroll.setWidget(self.chat_widget)
        self.tabs.addTab(self.chat_scroll, "对话")
        self.attachment_list = QListWidget()
        self.attachment_list.itemDoubleClicked.connect(self.preview_attachment)
        self.tabs.addTab(self.attachment_list, "附件")
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.tabs.addTab(self.info, "会话信息")
        d.addWidget(self.tabs, 1)
        d.addWidget(label("✓ 源会话与附件保持原样 · 历史工具不会重新执行", "Muted"))
        split.addWidget(self.detail_frame)
        split.setSizes([350, 780])
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)

    def _replace_page(self, key):
        if key in self.pages:
            old = self.pages.pop(key)
            self.stack.removeWidget(old)
            old.deleteLater()
        return self._page(key)

    def show_welcome(self):
        page, layout = self._replace_page("welcome")
        layout.addStretch()
        title = label("连接你的会话资料", "Heading")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        subtitle = label("选择来源后，即可在本地查看、整理和备份。", "Subheading")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(25)
        cards = QHBoxLayout()
        for provider, name in PROVIDERS.items():
            frame, content = card()
            content.addWidget(label(name, "Section"))
            content.addWidget(label("识别默认资料目录，或手动选择其他位置。", "Muted", True))
            content.addSpacing(12)
            content.addWidget(button("选择资料目录", lambda _=False, p=provider: self.add_source(p)))
            cards.addWidget(frame)
        layout.addLayout(cards)
        assurance = label("原始会话保持原样     ·     本地读取     ·     不自动恢复历史任务", "Good")
        assurance.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(assurance)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("打开已有迁移包", self.open_package))
        actions.addWidget(button("识别本机资料", self.scan, True))
        actions.addStretch()
        layout.addSpacing(16)
        layout.addLayout(actions)
        layout.addStretch()
        self.stack.setCurrentWidget(page)

    def task(self, fn, done, stage="处理中", failure=None):
        task = Task(fn)
        self.tasks.add(task)
        task.signals.progress.connect(lambda text, pct: self.status.setText(f"{text}  {pct}%"))
        def finish(value):
            self.tasks.discard(task)
            self.status.setText("本地资料 · 源会话保持原样")
            try:
                done(value)
            except Exception as error:
                self.loading = False
                self.error("界面未能展示这份资料，源文件未改变。\n" + type(error).__name__ + "：" + str(error))
        def fail(message, report=None):
            self.tasks.discard(task)
            self.last_failure_report = report
            if report:
                if report.get("schema") == "asm-native-import-1":
                    self.state.record_import(report)
                else:
                    self.state.record_backup(report)
            self.status.setText("操作未完成 · 请查看结果记录")
            if failure:
                failure(message)
            else:
                self.error(message)
        task.signals.done.connect(finish)
        task.signals.failed.connect(fail)
        self.status.setText(stage)
        self.pool.start(task)
        return task

    def scan(self):
        if self.loading:
            self.status.setText("正在读取来源，请稍候…")
            return
        if self.busy:
            self.error("备份仍在进行，请等任务结束或取消后再刷新来源。")
            return
        self.status.setText("正在识别本地资料…")
        self.loading = True
        self.library = LocalLibrary(self.home, self.state.cache, self.state.data.get("sources", []))
        library = self.library
        def failed(message):
            self.loading = False
            self.error(message)
        self.task(lambda progress: library.scan(progress), self.apply_catalog, "正在识别本地资料", failed)

    def apply_catalog(self, catalog):
        self.loading = False
        self.catalog, self.records = catalog, catalog.records
        self.record_map = {r.uid: r for r in self.records}
        self.source_filter, self.project_filter = "all", "all"
        self.projects.clear()
        counts = {}
        for r in self.records:
            if not r.child:
                counts[r.project] = counts.get(r.project, 0) + 1
        for title, count in sorted(counts.items(), key=lambda p: (-p[1], p[0])):
            item = QListWidgetItem(f"▱  {title}  {count}")
            item.setData(Qt.ItemDataRole.UserRole, title)
            self.projects.addItem(item)
        for provider, b in self.provider_buttons.items():
            b.setText(PROVIDERS[provider] + "   " + str(sum(r.provider == provider and not r.child for r in self.records)))
        self.navigate("library")
        if not self.records:
            self.detail_notice.setText("没有发现可读取的本地会话。可以添加其他资料目录，或打开已有迁移包。")
            self.detail_notice.show()
        if catalog.warnings:
            self.status.setText("部分来源需要关注；请在来源管理查看详情")

    def filtered(self):
        query = self.search.text().strip().casefold()
        status = self.filter_box.currentIndex()
        return [r for r in self.records if (self.source_filter == "all" or r.provider == self.source_filter) and (self.project_filter == "all" or r.project == self.project_filter) and (self.show_children.isChecked() or not r.child) and (not query or query in (r.title + " " + r.project + " " + PROVIDERS[r.provider]).casefold()) and (status == 0 or status == 1 and r.archived or status == 2 and bool(r.issue) or status == 3 and r.uid in self.marks)]

    def populate_sessions(self, *_):
        if not hasattr(self, "session_list"):
            return
        old = self.current_record.uid if self.current_record else ""
        self._building_list = True
        self.session_list.clear()
        current_item = None
        rows = self.filtered()
        for record in rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if record.uid in self.selected else Qt.CheckState.Unchecked)
            item.setToolTip(html.escape(record.title + "\n" + PROVIDERS[record.provider] + " · " + record.project))
            self.session_list.addItem(item)
            if record.uid == old:
                current_item = item
        self._building_list = False
        self.list_count.setText(f"{len(rows)} 条")
        self.selection_label.setText(f"{len(self.selected & self.record_map.keys())} 条已加入迁移清单")
        self.nav_buttons["transfer"].setText("⇄  迁移清单   " + str(len(self.selected & self.record_map.keys())))
        if rows:
            self.session_list.setCurrentItem(current_item or self.session_list.item(0))
        else:
            self.detail_title.setText("没有符合条件的会话")
            self.detail_subtitle.setText("试试其他关键词，或清除来源和状态筛选。")
            clear_layout(self.chat_layout)
            self.chat_layout.addWidget(label("没有符合条件的会话", "Muted"))
            self.chat_layout.addStretch()

    def filter_provider(self, provider):
        self.source_filter, self.project_filter = provider, "all"
        self.projects.clearSelection()
        self.navigate("library", keep_filters=True)

    def filter_project(self, item):
        self.project_filter, self.source_filter = item.data(Qt.ItemDataRole.UserRole), "all"
        self.navigate("library", keep_filters=True)

    def select_visible(self):
        rows = self.filtered()
        for r in rows:
            if self.select_all.isChecked():
                self.selected.add(r.uid)
            else:
                self.selected.discard(r.uid)
        self.save_selection()
        self.populate_sessions()

    def selection_changed(self, item):
        if self._building_list:
            return
        r = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(r.uid)
        else:
            self.selected.discard(r.uid)
        self.save_selection()
        self.selection_label.setText(f"{len(self.selected & self.record_map.keys())} 条已加入迁移清单")
        self.nav_buttons["transfer"].setText("⇄  迁移清单   " + str(len(self.selected & self.record_map.keys())))

    def save_selection(self):
        self.state.data["selection"] = sorted(self.selected)
        self.state.save()

    def choose_session(self, item, previous=None):
        if self._building_list or item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        self.current_record = record
        self.limit = 80
        self.detail_title.setText(record.title)
        self.detail_subtitle.setText(PROVIDERS[record.provider] + " · " + record.project + (" · 原客户端已归档" if record.archived else "") + (" · 数据包预览" if record.metadata.get("preview_only") else ""))
        self.star.setText("★" if record.uid in self.marks else "☆")
        self.copy_selector.blockSignals(True)
        self.copy_selector.clear()
        if record.provider == "claude" and len(record.files) > 1:
            for p in record.files:
                self.copy_selector.addItem("日志副本：" + str(Path(p).parent.name), p)
            self.copy_selector.show()
        else:
            self.copy_selector.hide()
        self.copy_selector.blockSignals(False)
        self.detail_notice.setText(record.issue)
        self.detail_notice.setVisible(bool(record.issue))
        self.info.setPlainText("来源：" + PROVIDERS[record.provider] + "\n\n会话标识：" + record.session_id + "\n\n原项目目录：" + (record.cwd or "未记录") + "\n\n原始日志副本：" + str(len(record.files)) + "\n\n归档状态：" + ("原客户端已归档" if record.archived else "未归档") + "\n\n目标机：Windows 导入与真实续聊待验证\n\n查看历史不会恢复旧权限、Goals、队列或定时任务。\n\n高级信息：\n" + json.dumps(record.metadata, ensure_ascii=False, indent=2, default=str))
        self.load_page()

    def change_copy(self, index):
        if self.current_record and index >= 0:
            self.current_record.metadata["primary_file"] = self.copy_selector.itemData(index)
            self.load_page()

    def load_page(self):
        if not self.current_record:
            return
        self.detail_generation += 1
        self.detail_cancel.set()
        self.detail_cancel = threading.Event()
        cancel = self.detail_cancel
        generation, record, library, limit = self.detail_generation, self.current_record, self.library, self.limit
        clear_layout(self.chat_layout)
        self.chat_layout.addWidget(label("正在读取历史记录…", "Muted"))
        self.chat_layout.addStretch()
        self.task(lambda _: library.page(record, limit, cancel), lambda page: self.display_page(page) if generation == self.detail_generation else None, "正在读取会话历史", lambda message: self.detail_error(message, generation))

    def detail_error(self, message, generation):
        if generation != self.detail_generation:
            return
        clear_layout(self.chat_layout)
        self.chat_layout.addWidget(label("无法展示这份历史。原始资料没有改变。\n" + message, "Notice", True))
        self.chat_layout.addStretch()

    def display_page(self, page):
        clear_layout(self.chat_layout)
        if page.total > len(page.messages):
            self.chat_layout.addWidget(button(f"加载更早记录 · 当前 {len(page.messages)} / {page.total}", self.load_earlier, quiet=True))
        for warning in page.warnings[:5]:
            self.chat_layout.addWidget(label(warning, "Notice", True))
        if not page.messages:
            self.chat_layout.addWidget(label("没有可展示的正文。原始日志仍保留，可查看会话信息或创建保全备份。", "Muted", True))
        for msg in page.messages:
            if msg.kind in {"thinking", "tool", "event"}:
                frame = QFrame()
                frame.setObjectName("ToolCard")
                content = QVBoxLayout(frame)
                content.setContentsMargins(10, 4, 10, 7)
                toggle = QToolButton()
                toggle.setText(("◇ " if msg.kind == "thinking" else "▱ ") + (msg.label or "历史记录"))
                toggle.setCheckable(True)
                toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                content.addWidget(toggle)
                holder = QWidget()
                holder_layout = QVBoxLayout(holder)
                holder_layout.setContentsMargins(4, 0, 4, 0)
                holder.hide()
                content.addWidget(holder)
                def expand(checked, target=holder, lay=holder_layout, message=msg):
                    if checked and lay.count() == 0:
                        lay.addWidget(SafeText(message.text, plain=True))
                    target.setVisible(checked)
                toggle.toggled.connect(expand)
                self.chat_layout.addWidget(frame)
            else:
                frame = QFrame()
                frame.setObjectName("UserBubble" if msg.role == "user" else "Assistant")
                content = QVBoxLayout(frame)
                content.setContentsMargins(15, 12, 15, 12)
                content.addWidget(label("你" if msg.role == "user" else PROVIDERS[self.current_record.provider] if msg.role == "assistant" else "来源记录", "Muted"))
                content.addWidget(SafeText(msg.text))
                if msg.role == "user":
                    row = QHBoxLayout()
                    row.addSpacing(65)
                    row.addWidget(frame, 1)
                    self.chat_layout.addLayout(row)
                else:
                    self.chat_layout.addWidget(frame)
        self.chat_layout.addStretch()
        self.attachment_list.clear()
        for attachment in page.attachments:
            text = attachment.name + ("\n" + attachment.note if attachment.note else "\n双击预览 · 原始文件保持不变")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, attachment)
            self.attachment_list.addItem(item)
        self.tabs.setTabText(1, "附件 " + str(len(page.attachments)))
        QTimer.singleShot(30, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def load_earlier(self):
        self.limit += 100
        self.load_page()

    def toggle_mark(self):
        if self.current_record:
            uid = self.current_record.uid
            self.marks.discard(uid) if uid in self.marks else self.marks.add(uid)
            self.state.data["marks"] = sorted(self.marks)
            self.state.save()
            self.star.setText("★" if uid in self.marks else "☆")
            if self.filter_box.currentIndex() == 3:
                self.populate_sessions()

    def add_current(self):
        if self.current_record:
            self.selected.add(self.current_record.uid)
            self.save_selection()
            self.populate_sessions()
            self.status.setText("已加入迁移清单；源会话未改变")

    def preview_attachment(self, item):
        attachment = item.data(Qt.ItemDataRole.UserRole)
        dialog = QDialog(self)
        dialog.setWindowTitle(attachment.name)
        dialog.resize(780, 600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(attachment.name, "Section"))
        image = QPixmap()
        buffer = None
        if attachment.data:
            try:
                data = base64.b64decode(attachment.data, validate=True)
                if len(data) <= 24 * 1024 * 1024:
                    buffer = QBuffer()
                    buffer.setData(QByteArray(data))
                    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    reader = QImageReader(buffer)
                else:
                    reader = None
            except ValueError:
                reader = None
        elif attachment.path and Path(attachment.path).is_file():
            reader = QImageReader(attachment.path)
        else:
            reader = None
        if reader is not None:
            reader.setDecideFormatFromContent(True)
            reader.setAutoTransform(True)
            fmt = bytes(reader.format()).decode("ascii", errors="ignore").lower()
            size = reader.size() if fmt in {"png", "jpeg", "jpg", "webp", "gif", "bmp", "ico", "tiff", "tif", "icns"} else QSize()
            if size.isValid() and size.width() * size.height() <= 64_000_000:
                reader.setScaledSize(size.scaled(1400, 1000, Qt.AspectRatioMode.KeepAspectRatio))
                image = QPixmap.fromImage(reader.read())
        if not image.isNull():
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setPixmap(image.scaled(730, 485, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(preview, 1)
        elif attachment.path and Path(attachment.path).suffix.lower() in {".txt", ".md", ".json", ".csv", ".log"}:
            body = QPlainTextEdit()
            body.setReadOnly(True)
            with open(attachment.path, "rb") as f:
                body.setPlainText(f.read(256 * 1024).decode("utf-8", errors="replace"))
            layout.addWidget(body, 1)
        else:
            layout.addWidget(label(attachment.note or "此文件保留在原始位置。当前版本只预览图片和文本，不执行或启动附件。", "Muted", True), 1)
        layout.addWidget(label("本地预览 · 不执行文件，不自动联网加载资源", "Muted"))
        layout.addWidget(button("关闭", dialog.accept))
        dialog.exec()

    def navigate(self, route, keep_filters=False):
        if self.busy and route not in {"progress"}:
            self.error("备份任务仍在运行，请等待或在进度页取消。")
            return
        if route == "library":
            if not keep_filters:
                self.source_filter, self.project_filter = "all", "all"
            self.populate_sessions()
        elif route == "sources":
            self.build_sources()
        elif route == "transfer":
            self.build_transfer()
        elif route == "backups":
            self.build_backups()
        elif route == "imports":
            self.build_imports()
        self.nav_buttons.get(route, self.nav_buttons["library"]).setChecked(True)
        self.stack.setCurrentWidget(self.pages[route])

    def build_sources(self):
        page, layout = self._replace_page("sources")
        self._heading(layout, "来源管理", "连接本地资料；不会启动原客户端或恢复历史任务", [button("添加来源", self.add_source, True), button("重新识别", self.scan)])
        for source in self.catalog.sources if self.catalog else []:
            frame, content = card()
            row = QHBoxLayout()
            row.addWidget(label(PROVIDERS[source["provider"]], "Section"), 1)
            row.addWidget(label(source.get("status", ""), "Good" if source.get("status") == "可读取" else "Muted"))
            content.addLayout(row)
            content.addWidget(label(source["path"], "Muted", True))
            content.addWidget(label(str(source.get("count", 0)) + " 条主要会话 · " + str(source.get("total", source.get("count", 0))) + " 条含子任务的记录", "Subheading"))
            if source["provider"] == "claude":
                content.addWidget(label("读取 Claude Code / Desktop Code 的本地历史；普通云端聊天需由原账户同步或官方导出。", "Muted", True))
            if source.get("error"):
                content.addWidget(label(source["error"], "Notice", True))
            content.addWidget(button("查看会话", lambda _=False, p=source["provider"]: self.filter_provider(p)))
            layout.addWidget(frame)
        layout.addWidget(label("只包含本机可访问资料。尚未从云端下载的聊天与附件不在本地扫描范围内。", "Info", True))
        layout.addStretch()

    def build_transfer(self):
        page, layout = self._replace_page("transfer")
        self._heading(layout, "迁移清单", "这里的选择与标记保存在本工具中，不改写原客户端", [button("去选择会话", lambda: self.navigate("library")), button("新建备份", self.new_backup, True)])
        listing = QListWidget()
        for uid in self.selected:
            if uid in self.record_map:
                r = self.record_map[uid]
                item = QListWidgetItem(r.title + "\n" + PROVIDERS[r.provider] + " · " + r.project)
                item.setData(Qt.ItemDataRole.UserRole, uid)
                listing.addItem(item)
        layout.addWidget(listing, 1)
        def remove():
            for item in listing.selectedItems():
                self.selected.discard(item.data(Qt.ItemDataRole.UserRole))
            self.save_selection()
            self.build_transfer()
            self.stack.setCurrentWidget(self.pages["transfer"])
        layout.addWidget(button("从迁移清单移除选中项", remove))
        layout.addWidget(label("移出清单不会删除源会话，也不会改变原客户端的归档与置顶。", "Muted"))

    def add_source(self, provider=None):
        if self.busy:
            self.error("请先等待当前备份任务结束。")
            return
        if provider is None or isinstance(provider, bool):
            name, ok = QInputDialog.getItem(self, "添加来源", "选择客户端", list(PROVIDERS.values()), 0, False)
            if not ok:
                return
            provider = next(k for k, v in PROVIDERS.items() if v == name)
        path = QFileDialog.getExistingDirectory(self, "选择 " + PROVIDERS[provider] + " 的资料目录", str(self.home))
        if not path:
            return
        root = Path(path).resolve()
        if root == self.home.resolve() or root == Path(root.anchor) or within(self.state.root, root) or within(self.state.cache, root):
            self.error("请选择客户端自己的资料目录，不能选择用户主目录、磁盘根目录或本工具资料目录。")
            return
        self.state.data.setdefault("sources", []).append({"provider": provider, "path": str(root)})
        self.state.save()
        self.scan()

    def open_package(self):
        if self.loading:
            self.status.setText("请先等待读取当前来源完成")
            return
        if self.busy:
            self.error("请先等待当前备份任务结束。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "打开备份或迁移包 · 只读预览", "", "备份与迁移包 (*.zip *.asmb *.enc)")
        if not path:
            return
        key_path = auth_path = None
        if Path(path).suffix in {".asmb", ".enc"}:
            key_path, _ = QFileDialog.getOpenFileName(self, "选择单独保存的恢复密钥", "", "恢复密钥 (*.asmkey *.key);;所有文件 (*)")
            if not key_path:
                return
            if Path(path).suffix == ".enc":
                auth_path = Path(path).parent / "backup-auth.json"
                if not auth_path.is_file():
                    auth_name, _ = QFileDialog.getOpenFileName(self, "选择这份旧备份的 backup-auth.json", "", "认证清单 (*.json)")
                    if not auth_name:
                        return
                    auth_path = Path(auth_name)
        self.loading = True
        def load(progress):
            source = decrypt_for_preview(Path(path), Path(key_path), self.state.cache / "decrypted", progress, auth_path) if key_path else Path(path)
            library = PackageLibrary(source, self.state.cache)
            catalog = library.scan(progress)
            return library, catalog
        def done(value):
            self.library, catalog = value
            self.apply_catalog(catalog)
            self.status.setText("迁移包只读预览 · 不写入原客户端 · 数据对象按需校验")
        def failed(message):
            self.loading = False
            self.error(message)
        self.task(load, done, "正在读取迁移包目录", failed)

    def new_backup(self):
        if self.loading:
            self.error("请等来源读取完成后再选择备份范围。")
            return
        if self.busy:
            self.error("已有备份任务正在进行。")
            return
        if not self.records:
            self.error("请先识别本地资料或打开已有迁移包。")
            return
        self.wizard = BackupWizard(self)
        self.wizard.exec()

    def new_import(self):
        if self.busy or self.loading:
            self.error("请先等待当前任务结束。")
            return
        if not isinstance(self.library, PackageLibrary):
            QMessageBox.information(self, "先打开迁移包", "请先点击“打开迁移包”，在包内勾选会话，然后点击“导入到本机”。\n\n导入向导会先预览冲突并完整备份目标资料。")
            return
        self.import_wizard = ImportWizard(self)
        self.import_wizard.exec()

    def run_import(self, plan, backup_directory):
        self.busy = True
        self.cancel = threading.Event()
        page, layout = self._replace_page("progress")
        self._heading(layout, "正在恢复到本机", "完整目标备份 → 副本恢复 → 核验 → 发布；原会话 ID 冲突跳过")
        frame, content = card()
        self.backup_stage = label("准备目标备份", "Section")
        self.backup_progress = QProgressBar()
        content.addWidget(self.backup_stage); content.addWidget(self.backup_progress)
        content.addWidget(label("原目标目录和完整备份均保留。发布阶段会完成当前安全边界后再响应取消。", "Info", True))
        content.addWidget(button("取消任务", self.cancel_backup))
        layout.addWidget(frame); layout.addStretch()
        self.stack.setCurrentWidget(page)
        def done(report):
            self.busy = False
            self.state.record_import(report)
            self.show_import_result(report)
        def failed(message):
            self.busy = False
            report = getattr(self, "last_failure_report", None)
            if report and report.get("schema") == "asm-native-import-1":
                self.show_import_result(report)
            else:
                self.error(message)
                self.navigate("imports")
        task = self.task(lambda progress: execute_import(plan, backup_directory, self.state.cache, confirmed_offline=True, progress=progress, cancel=self.cancel), done, "正在备份目标资料", failed)
        task.signals.progress.connect(self.backup_update)

    def build_imports(self):
        page, layout = self._replace_page("imports")
        self._heading(layout, "导入记录", "完整目标备份和恢复日志保留在本机", [button("打开恢复记录", self.choose_import_result), button("打开迁移包", self.open_package)])
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        holder = QWidget(); rows = QVBoxLayout(holder)
        for report in self.state.data.get("imports", []):
            frame, content = card()
            content.addWidget(label(Path(report.get("output") or "导入恢复").name, "Section"))
            status = {"complete": "原生资料已导入，待原客户端和续聊验证", "rolled-back": "已恢复到导入前", "needs-attention": "需要检查恢复记录"}.get(report.get("status"), "未完成，可检查恢复")
            content.addWidget(label(status, "Muted", True))
            content.addWidget(button("查看结果 / 检查恢复", lambda _=False, r=report: self.open_import_result(r)))
            rows.addWidget(frame)
        if not self.state.data.get("imports"):
            rows.addWidget(label("打开迁移包后，可从会话库进入“导入到本机”。", "Muted", True))
        rows.addStretch(); scroll.setWidget(holder); layout.addWidget(scroll)

    def open_import_result(self, report):
        try:
            from .safeio import stable_bytes
            full = json.loads(stable_bytes(Path(report["journal"]), 256 * 1024 * 1024))
            if full.get("schema") != "asm-native-import-1":
                raise ValueError("不是本工具的导入恢复记录")
            self.state.record_import(full)
            self.show_import_result(full)
        except Exception as error:
            self.error("无法读取恢复记录：" + str(error))

    def choose_import_result(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开导入恢复记录 · 异常退出后可从备份目录选择", "", "导入恢复记录 (*.json)")
        if path:
            self.open_import_result({"journal": path})

    def show_import_result(self, report):
        page, layout = self._replace_page("import-result")
        state = report.get("status")
        title = "会话资料已导入" if state == "complete" else "已恢复导入前资料" if state == "rolled-back" else "导入需要关注"
        self._heading(layout, title, "原客户端显示与真实续聊需在目标电脑另行验证")
        frame, content = card()
        content.addWidget(label(f"本次计划新增 {report.get('imported', 0)} 条 · 跳过 {report.get('skipped', 0)} 条", "Section"))
        for text in ["目标资料完整备份：" + ("已回读校验" if report.get("backupVerified") else "未完成"), "导入副本核验：" + ("通过" if report.get("targetReadbackVerified") else "未完成"), "撤销核验：" + ("通过" if report.get("rollbackVerified") else "未执行"), "原包、源会话和 thinking 保留；未调用模型", "Windows 真实导入、跨账号/组织/供应商兼容与续聊：待新电脑验证"]:
            content.addWidget(label(text, "Muted", True))
        content.addWidget(label("备份与恢复日志：" + str(report.get("output", "")), "Info", True))
        content.addWidget(label("目标完整备份含目标机原有私密资料，未加密。原目标目录和撤销后的副本均保留；此版不自动清理这些资料。", "Notice", True))
        if report.get("error") or report.get("recoveryError"):
            content.addWidget(SafeText(str(report.get("error", "")) + "\n" + str(report.get("recoveryError", "")), plain=True))
        if report.get("output"):
            content.addWidget(button("打开备份和恢复日志", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(report["output"]))))
        if state != "rolled-back" and report.get("roots"):
            content.addWidget(button("撤销本次导入 / 恢复到导入前", lambda: self.undo_import(report)))
        content.addWidget(button("查看导入记录", lambda: self.navigate("imports")))
        layout.addWidget(frame); layout.addStretch(); self.stack.setCurrentWidget(page)

    def undo_import(self, report):
        if self.busy or self.loading:
            return
        reply = QMessageBox.question(self, "确认撤销与恢复", "请先正常退出 Claude、Codex、DeepSeek Harness 及其后台任务。\n\n仅在目标仍与本次导入结果完全一致时恢复；若已有新对话，会停止并保留所有副本。\n\n已经退出这些客户端并开始恢复吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.busy = True
        def done(value):
            self.busy = False; self.state.record_import(value); self.show_import_result(value)
        def failed(message):
            self.busy = False; self.error(message)
        self.task(lambda progress: rollback_import(Path(report["journal"]), self.state.cache, confirmed_offline=True, progress=progress), done, "正在检查并恢复目标资料", failed)

    def run_backup(self, records, destination, key_path, include_projects=False):
        self.busy = True
        self.cancel = threading.Event()
        page, layout = self._replace_page("progress")
        self._heading(layout, "正在创建备份", "在线快照 · 原始会话与现有 thinking 保持原样")
        frame, content = card()
        self.backup_stage = label("准备备份", "Section")
        self.backup_progress = QProgressBar()
        self.backup_progress.setRange(0, 100)
        content.addWidget(self.backup_stage)
        content.addWidget(self.backup_progress)
        content.addWidget(label(f"{len(records)} 条会话 · {len({r.provider for r in records})} 个来源", "Muted"))
        content.addWidget(label("资料读取 → 原始归档 → 完整性校验 → 加密 → 实际解密回读", "Subheading", True))
        content.addWidget(label("校验与回读通过前不标记完成。取消或失败会保留已有结果与故障记录。", "Info", True))
        content.addWidget(button("取消任务", self.cancel_backup))
        layout.addWidget(frame)
        layout.addStretch()
        self.stack.setCurrentWidget(page)
        library = self.library
        def done(report):
            self.busy = False
            self.state.record_backup(report)
            self.show_result(report)
        def failed(message):
            self.busy = False
            self.error(message + "\n\n原始资料未改变。输出位置的部分归档和故障记录保留；失败目录可能含临时明文，请保管在私密位置。")
            if getattr(self, "last_failure_report", None):
                self.show_result(self.last_failure_report)
            else:
                self.navigate("backups")
        task = self.task(lambda progress: make_backup(library, records, destination, key_path, progress, self.cancel, {"marks": sorted(self.marks & {r.uid for r in records})}, include_projects), done, "正在创建备份", failed)
        task.signals.progress.connect(self.backup_update)

    def backup_update(self, stage, percent):
        if self.busy:
            self.backup_stage.setText(stage + "  " + str(percent) + "%")
            self.backup_progress.setValue(percent)

    def cancel_backup(self):
        if QMessageBox.question(self, "取消备份", "在安全边界停止，并保留已有结果与故障记录。是否取消？") == QMessageBox.StandardButton.Yes:
            self.cancel.set()

    def build_backups(self):
        page, layout = self._replace_page("backups")
        self._heading(layout, "备份库", "保存结果、校验与遗漏分别记录", [button("校验已有迁移包", self.check_package), button("新建备份", self.new_backup, True)])
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        items = QVBoxLayout(holder)
        for report in self.state.data.get("backups", []):
            frame, content = card()
            row = QHBoxLayout()
            row.addWidget(label(Path(report.get("output") or "备份").name, "Section"), 1)
            status = report.get("status", "未完成")
            row.addWidget(label("完成，需关注" if status == "complete-with-warnings" else "校验与回读通过" if status == "complete" else "未完成", "Good" if status == "complete" else "Muted"))
            content.addLayout(row)
            content.addWidget(label(f"{report.get('sessions', 0)} 条会话 · {human_size(report.get('bytes') or 0)} · 在线快照", "Muted"))
            content.addWidget(button("查看结果", lambda _=False, r=report: self.show_result(r)))
            items.addWidget(frame)
        if not self.state.data.get("backups"):
            items.addWidget(label("还没有在本工具中创建备份。可以从会话库选择内容，或校验已有迁移包。", "Muted", True))
        items.addStretch()
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

    def show_result(self, report):
        page, layout = self._replace_page("result")
        warnings = report.get("warnings") or []
        status = report.get("status", "failed")
        complete = status in {"complete", "complete-with-warnings"}
        self._heading(layout, "备份结果", "每一步都有结果，遗漏也清晰可见", [button("返回备份库", lambda: self.navigate("backups"))])
        layout.addWidget(label("备份已完成，有需要关注的内容" if complete and warnings else "备份与校验已完成" if complete else "备份尚未完成", "Notice" if warnings or not complete else "Info", True))
        frame, content = card()
        content.addWidget(label(f"{report.get('sessions', 0)} 条会话  ·  {report.get('files', 0)} 个数据对象  ·  {human_size(report.get('bytes') or 0)}", "Section", True))
        for text in ["逐文件完整性校验：通过" if complete else "逐文件完整性校验：未完成", "实际解密回读：通过" if report.get("encryptedReadbackVerified") else "实际解密回读：未完成", "原始会话与 thinking：未改写", "Windows 原生导入与真实续聊：待目标机验证", "本次为在线快照，不能替代来源退出后的最终备份"]:
            content.addWidget(label(text, "Good" if "通过" in text else "Muted", True))
        actions = QHBoxLayout()
        actions.addWidget(button("打开备份位置", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(report.get("output", "")))))
        if report.get("portable"):
            actions.addWidget(button("再次校验迁移包", lambda: self.check_package(report["portable"])))
        if report.get("encrypted") and report.get("plainSha256"):
            actions.addWidget(button("校验加密备份", lambda: self.check_encrypted(report)))
        content.addLayout(actions)
        layout.addWidget(frame)
        if not complete:
            layout.addWidget(label("未完成的暂存文件可能含明文历史，已保留在该任务目录。请私密保管，不要把它们当作完成的迁移包。", "Notice", True))
            if report.get("archiveReady") and Path(report.get("pendingArchive", "")).is_file():
                layout.addWidget(button("继续校验与加密", lambda: self.resume_gui(report), True))
            layout.addWidget(button("重新创建备份（保留旧任务）", self.new_backup))
        if warnings:
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setPlainText("需要关注：\n\n" + "\n".join(warnings))
            layout.addWidget(view, 1)
        else:
            layout.addStretch()
        self.stack.setCurrentWidget(page)

    def resume_gui(self, report):
        if self.busy:
            return
        key = report.get("keyPath", "")
        if not key or not Path(key).is_file():
            key, _ = QFileDialog.getOpenFileName(self, "选择该任务的恢复密钥", "", "恢复密钥 (*.asmkey)")
        if not key:
            return
        self.busy = True
        self.cancel = threading.Event()
        page, layout = self._replace_page("progress")
        self._heading(layout, "继续校验与加密", "复用已完成的归档，不再读取原始会话")
        self.backup_stage = label("准备校验", "Section")
        self.backup_progress = QProgressBar()
        layout.addWidget(self.backup_stage)
        layout.addWidget(self.backup_progress)
        layout.addWidget(button("取消任务", self.cancel_backup))
        layout.addStretch()
        self.stack.setCurrentWidget(page)
        def done(value):
            self.busy = False
            self.state.record_backup(value)
            self.show_result(value)
        def failed(message):
            self.busy = False
            self.error(message)
            self.show_result(self.last_failure_report or report)
        roots = [s["path"] for s in self.library.catalog.sources]
        task = self.task(lambda progress: resume_backup(report, Path(key), progress, self.cancel, roots), done, "正在校验续接归档", failed)
        task.signals.progress.connect(self.backup_update)

    def check_package(self, path=None):
        if not path or isinstance(path, bool):
            path, _ = QFileDialog.getOpenFileName(self, "选择需要校验的迁移包", "", "会话迁移包 (*.zip)")
        if path:
            self.task(lambda progress: verify_package(Path(path), progress), lambda result: QMessageBox.information(self, "校验通过", f"全部 {result['files']} 个对象的大小与 SHA-256 均匹配。\n尚未执行原生导入或续聊。"), "正在完整性校验")

    def check_encrypted(self, report):
        path = report.get("keyPath")
        if not path or not Path(path).is_file():
            path, _ = QFileDialog.getOpenFileName(self, "选择单独保存的恢复密钥", "", "恢复密钥 (*.asmkey)")
        if path:
            self.task(lambda progress: verify_encrypted(Path(report["encrypted"]), Path(path), report["plainSha256"], progress), lambda _: QMessageBox.information(self, "回读通过", "实际解密回读与原始归档哈希一致。"), "正在实际解密回读")

    def show_settings(self):
        if self.busy:
            self.error("备份进行时暂不更改缓存位置。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("设置")
        dialog.resize(570, 310)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("本工具的资料", "Section"))
        layout.addWidget(label(str(self.state.root), "Muted", True))
        layout.addWidget(label("缓存位置", "Section"))
        cache_label = label(str(self.state.cache), "Muted", True)
        layout.addWidget(cache_label)
        def change():
            path = QFileDialog.getExistingDirectory(dialog, "选择缓存存放位置")
            if not path:
                return
            target = Path(path) / "AI Session Migration Cache"
            if any(within(target, Path(s["path"])) for s in self.library.discover()):
                self.error("缓存必须保存在来源资料之外。")
                return
            self.state.data["cache"] = str(target)
            self.state.save()
            cache_label.setText(str(target))
        layout.addWidget(button("更改缓存位置", change))
        layout.addWidget(label("缓存只用于独立副本和迁移包预览，不向原客户端写入资料。", "Info", True))
        layout.addWidget(button("关闭", dialog.accept))
        dialog.exec()

    def show_about(self):
        QMessageBox.about(self, "关于", f"AI Session Migration {__version__}\n\nClaude · Codex · DeepSeek Harness\n本地查看、管理、备份与校验\n\nMIT License · Qt for Python (LGPLv3)\n原始资料与 thinking 保持不变\nWindows 导入与真实续聊待目标机验证")

    def error(self, message):
        box = QMessageBox(self)
        box.setWindowTitle("需要关注")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(message)
        box.exec()

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.information(self, "任务仍在进行", "请先等待当前资料任务结束，或在进度页取消任务。")
            event.ignore()
        elif self.tasks:
            self.detail_generation += 1
            self.detail_cancel.set()
            event.ignore()
            self.status.setText("正在安全结束读取，随后关闭…")
            if not self._closing:
                self._closing = True
                QTimer.singleShot(150, self._close_when_idle)
        else:
            event.accept()

    def _close_when_idle(self):
        if not self.tasks:
            self.close()
        else:
            QTimer.singleShot(150, self._close_when_idle)


class ImportWizard(QDialog):
    def __init__(self, owner: MainWindow):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("导入到本机 · 先备份并预览冲突")
        self.resize(1040, 780)
        self.step = 0
        self.plan = None
        self.working = False
        self.preview_cancel = threading.Event()
        self.target_fields, self.mapping_fields = {}, {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 23, 26, 22)
        outer.addWidget(label("恢复会话到这台电脑", "Heading"))
        outer.addWidget(label("① 选择会话    →    ② 目标与路径映射    →    ③ 冲突预览与确认", "Subheading"))
        self.pages = QStackedWidget()
        first = QWidget(); f = QVBoxLayout(first)
        f.addWidget(label("只选择需要导入的包内会话；同 ID 冲突默认跳过。", "Info", True))
        self.choices = QListWidget()
        for record in owner.records:
            item = QListWidgetItem(PROVIDERS[record.provider] + " · " + record.title)
            item.setData(Qt.ItemDataRole.UserRole, record.uid)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if record.uid in owner.selected else Qt.CheckState.Unchecked)
            self.choices.addItem(item)
        self.choices.itemChanged.connect(self.update_gate)
        f.addWidget(self.choices, 1)
        row = QHBoxLayout()
        row.addWidget(button("选择全部", lambda: self.choose_all(True)))
        row.addWidget(button("清除选择", lambda: self.choose_all(False)))
        row.addStretch(); f.addLayout(row)
        f.addWidget(label("Claude 仅恢复本地 Claude Code 历史，不是云端聊天导入。Codex 目标需先由原客户端初始化数据库，再正常退出。", "Muted", True))
        self.pages.addWidget(first)
        second = QWidget(); s = QVBoxLayout(second)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        holder = QWidget(); self.paths_layout = QVBoxLayout(holder)
        self.paths_layout.addWidget(label("选择原客户端的资料目录", "Section"))
        variables = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "deepseek": "DSH_HOME"}
        folders = {"claude": ".claude", "codex": ".codex", "deepseek": ".dsh"}
        for provider, name in PROVIDERS.items():
            self.paths_layout.addWidget(label(name, "Muted"))
            field = QLineEdit(str(Path(os.environ.get(variables[provider], str(owner.home / folders[provider]))).expanduser()))
            self.target_fields[provider] = field
            self._directory_row(self.paths_layout, field, "选择 " + name + " 目标资料目录")
        self.paths_layout.addWidget(label("Claude Desktop Code 索引目录（可选）", "Muted"))
        desktop = QLineEdit()
        desktop.setPlaceholderText("选择目标账号下 local_*.json 所在目录；留空仅恢复 Code 本地日志")
        self.target_fields["claude_desktop"] = desktop
        self._directory_row(self.paths_layout, desktop, "选择 Claude Desktop Code 目标索引容器")
        self.paths_layout.addWidget(label("项目路径映射", "Section"))
        self.paths_layout.addWidget(label("选择目标机已有的项目文件夹。这里仅映射会话归属，不写入、替换或执行项目工作文件。", "Muted", True))
        for cwd in sorted({r.cwd for r in owner.records}):
            self.paths_layout.addWidget(label(cwd or "原会话未记录项目", "Muted", True))
            default = cwd if cwd and Path(cwd).is_absolute() and Path(cwd).is_dir() and not cwd.startswith(("\\\\", "//")) else ""
            field = QLineEdit(default)
            field.setPlaceholderText("选择目标机已存在的项目目录")
            self.mapping_fields[cwd] = field
            self._directory_row(self.paths_layout, field, "选择目标项目目录")
        self.paths_layout.addWidget(label("目标资料完整备份与恢复记录位置", "Section"))
        self.destination = QLineEdit(str(owner.state.root / "导入备份"))
        self._directory_row(self.paths_layout, self.destination, "选择目标完整备份位置")
        self.paths_layout.addWidget(label("请先退出目标原客户端及后台任务再预览；目标变化会使预览失效。恢复需要目标磁盘有一份独立副本空间，备份位置另需完整目标资料空间。", "Notice", True))
        self.paths_layout.addStretch(); scroll.setWidget(holder); s.addWidget(scroll)
        self.pages.addWidget(second)
        third = QWidget(); t = QVBoxLayout(third)
        self.summary = label("尚未预览", "Section", True); t.addWidget(self.summary)
        self.preview = QListWidget(); t.addWidget(self.preview, 1)
        self.preview_notice = label("预览不会导入会话。", "Info", True); t.addWidget(self.preview_notice)
        self.offline = QCheckBox("我已正常退出所选目标客户端及其后台任务，并会保持退出直到导入完成")
        self.private_backup = QCheckBox("我理解完整目标备份含私密资料且未加密，已选择私密且空间充足的位置")
        self.offline.toggled.connect(self.update_gate); self.private_backup.toggled.connect(self.update_gate)
        t.addWidget(self.offline); t.addWidget(self.private_backup)
        t.addWidget(label("确认后先完整备份并回读，再恢复独立副本并发布。原目标目录和撤销后的副本保留；不覆盖同 ID 对话。真实 Windows 与续聊仍待新电脑验证。", "Muted", True))
        self.pages.addWidget(third); outer.addWidget(self.pages, 1)
        self.error_notice = label("", "Error", True); outer.addWidget(self.error_notice)
        actions = QHBoxLayout(); self.previous = button("取消", self.back); self.next = button("下一步", self.forward, True)
        actions.addWidget(self.previous); actions.addStretch(); actions.addWidget(self.next); outer.addLayout(actions)
        self.update_gate()

    def _directory_row(self, layout, field, title):
        row = QHBoxLayout(); row.addWidget(field, 1)
        def choose():
            value = QFileDialog.getExistingDirectory(self, title, field.text() or str(self.owner.home))
            if value:
                field.setText(value)
        row.addWidget(button("选择目录", choose)); layout.addLayout(row)

    def selected(self):
        return [self.owner.record_map[self.choices.item(i).data(Qt.ItemDataRole.UserRole)] for i in range(self.choices.count()) if self.choices.item(i).checkState() == Qt.CheckState.Checked]

    def choose_all(self, on):
        for i in range(self.choices.count()):
            self.choices.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def update_gate(self, *_):
        if not hasattr(self, "next"):
            return
        enabled = bool(self.selected()) if self.step == 0 else True if self.step == 1 else bool(self.plan and self.plan.ready and self.offline.isChecked() and self.private_backup.isChecked())
        self.next.setEnabled(enabled and not self.working)
        self.previous.setEnabled(True)

    def back(self):
        if self.working:
            self.preview_cancel.set()
            self.error_notice.setText("正在安全停止预览检查…")
            return
        if self.step == 0:
            self.reject(); return
        self.step -= 1; self.pages.setCurrentIndex(self.step)
        self.previous.setText("取消" if self.step == 0 else "上一步")
        self.next.setText("下一步" if self.step == 0 else "预览冲突")
        self.plan = None; self.error_notice.setText(""); self.update_gate()

    def forward(self):
        if self.working:
            return
        if self.step == 0:
            self.step = 1; self.pages.setCurrentIndex(1)
            self.previous.setText("上一步"); self.next.setText("预览冲突"); self.update_gate(); return
        if self.step == 1:
            selected = self.selected()
            targets = {p: f.text().strip() for p, f in self.target_fields.items()}
            mapping = {cwd: f.text().strip() for cwd, f in self.mapping_fields.items()}
            self.working = True; self.owner.loading = True; self.update_gate()
            self.preview_cancel = threading.Event()
            self.previous.setText("取消检查")
            self.pages.setEnabled(False)
            self.error_notice.setText("正在校验迁移包并检查目标资料，不写入客户端…")
            def done(plan):
                self.working = False; self.owner.loading = False; self.pages.setEnabled(True)
                self.plan = plan; self.step = 2; self.pages.setCurrentIndex(2)
                self.previous.setText("上一步")
                self.next.setText("备份目标并导入"); self.error_notice.setText("")
                self.summary.setText(f"可新增 {len(plan.ready)} 条 · 跳过或阻止 {len(plan.items) - len(plan.ready)} 条")
                self.preview.clear()
                for item in plan.items:
                    state = {"import": "新增", "skip": "跳过", "blocked": "阻止"}[item["action"]]
                    text = state + " · " + PROVIDERS[item["record"]["provider"]] + " · " + item["record"]["title"] + "\n" + item["reason"]
                    if item["action"] == "import":
                        text += "\n目标：" + plan.targets[item["record"]["provider"]] + "\n项目：" + item["cwd"]
                    self.preview.addItem(text)
                self.preview_notice.setText("目标备份位置：" + self.destination.text() + "\n\n" + "\n".join(plan.warnings))
                self.offline.setChecked(False); self.private_backup.setChecked(False); self.update_gate()
            def failed(message):
                self.working = False; self.owner.loading = False; self.pages.setEnabled(True)
                self.previous.setText("上一步")
                self.error_notice.setText(message); self.update_gate()
            destination_value = Path(self.destination.text().strip())
            def inspect(progress):
                plan = build_plan(self.owner.library, selected, targets, mapping, self.owner.state.cache, progress, self.preview_cancel)
                validate_backup_location(plan, destination_value, self.owner.state.cache)
                return plan
            task = self.owner.task(inspect, done, "正在预览导入冲突", failed)
            task.signals.progress.connect(lambda stage, percent: self.error_notice.setText(stage + "  " + str(percent) + "%"))
            return
        if not self.plan or not self.next.isEnabled():
            return
        plan = self.plan; destination = Path(self.destination.text().strip())
        self.accept(); self.owner.run_import(plan, destination)

    def reject(self):
        if not self.working:
            super().reject()


class BackupWizard(QDialog):
    def __init__(self, owner: MainWindow):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("新建备份 · 原始资料保持不变")
        self.resize(1000, 720)
        self.step = 0
        self.key_path = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 23, 26, 22)
        outer.addWidget(label("新建备份", "Heading"))
        self.steps = label("① 选择内容    →    ② 保存与保护    →    ③ 确认并开始", "Subheading")
        outer.addWidget(self.steps)
        self.pages = QStackedWidget()
        first = QWidget()
        f = QVBoxLayout(first)
        f.setContentsMargins(0, 12, 0, 0)
        f.addWidget(label("选择需要保留的会话", "Section"))
        self.choices = QListWidget()
        for record in owner.records:
            if record.child and record.uid not in owner.selected:
                continue
            item = QListWidgetItem(record.title + "\n" + PROVIDERS[record.provider] + " · " + record.project)
            item.setData(Qt.ItemDataRole.UserRole, record)
            item.setCheckState(Qt.CheckState.Checked if record.uid in owner.selected else Qt.CheckState.Unchecked)
            self.choices.addItem(item)
        self.choices.itemChanged.connect(self.update_gate)
        f.addWidget(self.choices, 1)
        choose = QHBoxLayout()
        choose.addWidget(button("全选此列表", lambda: self.choose_all(True)))
        choose.addWidget(button("清除选择", lambda: self.choose_all(False)))
        f.addLayout(choose)
        f.addWidget(label("这里只选择备份范围，不改变原客户端的会话或归档。", "Muted"))
        self.pages.addWidget(first)
        second = QWidget()
        s = QVBoxLayout(second)
        s.setContentsMargins(0, 12, 0, 0)
        self.summary = label("", "Section", True)
        s.addWidget(self.summary)
        location, l = card()
        l.addWidget(label("保存位置", "Section"))
        row = QHBoxLayout()
        self.destination = QLineEdit()
        self.destination.setReadOnly(True)
        row.addWidget(self.destination, 1)
        row.addWidget(button("选择文件夹", self.choose_destination))
        l.addLayout(row)
        s.addWidget(location)
        self.include_projects = QCheckBox("同时保存会话工作目录文件（可选，可能较大）")
        s.addWidget(self.include_projects)
        s.addWidget(label("保存会话工作目录中的未提交和未跟踪文件；不含其他关联根目录、Git 元数据、依赖缓存、独立凭据与密钥。", "Muted", True))
        key, k = card()
        k.addWidget(label("恢复密钥 · 单独保存", "Section"))
        k.addWidget(label("密钥用于恢复加密备份。请保存在本机或其他位置，不要放进备份文件夹。", "Muted", True))
        self.key_label = label("尚未保存恢复密钥", "Muted", True)
        k.addWidget(self.key_label)
        k.addWidget(button("保存恢复密钥", self.choose_key, True))
        self.key_confirm = QCheckBox("我已单独保存恢复密钥")
        self.key_confirm.setEnabled(False)
        self.key_confirm.toggled.connect(self.update_gate)
        k.addWidget(self.key_confirm)
        s.addWidget(key)
        s.addWidget(label("Windows 迁移包不包含独立登录凭据。历史和附件仍可能含私密信息，请私下保管迁移包。", "Info", True))
        s.addStretch()
        self.pages.addWidget(second)
        third = QWidget()
        t = QVBoxLayout(third)
        t.setContentsMargins(0, 12, 0, 0)
        t.addWidget(label("确认并创建在线快照", "Section"))
        self.final_summary = label("", "Subheading", True)
        t.addWidget(self.final_summary)
        t.addWidget(label("会生成：\n\n• 加密的所选会话保全备份\n• Windows 会话迁移包\n• 实际校验结果与文件清单", "Info", True))
        t.addWidget(label("首版在来源仍运行时创建在线快照。每个文件取得稳定副本、数据库在私有副本上读取，但不构成全应用同一时点的冷备份。不会关闭应用、恢复旧任务或调用模型。", "Notice", True))
        self.project_summary = label("", "Muted", True)
        t.addWidget(self.project_summary)
        t.addWidget(label("子任务与审批记录是独立会话，如需一并保全，请在列表中勾选。", "Muted", True))
        t.addWidget(label("Windows 原生导入、账号/供应商兼容与真实续聊将在目标电脑另行验证。", "Muted", True))
        t.addStretch()
        self.pages.addWidget(third)
        outer.addWidget(self.pages, 1)
        foot = QHBoxLayout()
        self.previous = button("取消", self.back)
        foot.addWidget(self.previous)
        foot.addStretch()
        self.next = button("下一步", self.forward, True)
        foot.addWidget(self.next)
        outer.addLayout(foot)
        self.update_gate()

    def selected(self):
        return [self.choices.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.choices.count()) if self.choices.item(i).checkState() == Qt.CheckState.Checked]

    def choose_all(self, on):
        for i in range(self.choices.count()):
            self.choices.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "选择备份保存位置")
        if path:
            if any(within(Path(path), Path(s["path"])) for s in self.owner.library.catalog.sources):
                self.owner.error("备份必须保存在源资料目录之外。")
                return
            self.destination.setText(path)
            self.update_gate()

    def choose_key(self):
        default = str(self.owner.state.root / ("recovery-" + time.strftime("%Y%m%d-%H%M%S") + ".asmkey"))
        path, _ = QFileDialog.getSaveFileName(self, "单独保存恢复密钥", default, "恢复密钥 (*.asmkey)")
        if not path:
            return
        if self.destination.text() and within(Path(path), Path(self.destination.text())):
            self.owner.error("请把恢复密钥保存到备份位置之外。")
            return
        if any(within(Path(path), Path(s["path"])) for s in self.owner.library.catalog.sources):
            self.owner.error("恢复密钥不能保存在源资料目录中。")
            return
        try:
            save_recovery_key(Path(path))
        except FileExistsError:
            self.owner.error("该文件已存在，请选择新的名称；工具不会覆盖已有密钥。")
            return
        except Exception as e:
            self.owner.error(str(e))
            return
        self.key_path = Path(path)
        self.key_label.setText("✓ 已保存并回读校验：" + path)
        self.key_label.setObjectName("Good")
        self.key_confirm.setEnabled(True)
        self.update_gate()

    def update_gate(self, *_):
        if not hasattr(self, "next"):
            return
        count = len(self.selected())
        self.summary.setText(f"已选择 {count} 条会话 · {len({r.provider for r in self.selected()})} 个来源")
        if self.step == 0:
            self.next.setEnabled(count > 0)
        elif self.step == 1:
            self.next.setEnabled(bool(self.destination.text()) and self.key_path is not None and self.key_confirm.isChecked())
        else:
            self.next.setEnabled(True)

    def back(self):
        if self.step == 0:
            self.reject()
            return
        self.step -= 1
        self.pages.setCurrentIndex(self.step)
        self.previous.setText("取消" if self.step == 0 else "上一步")
        self.next.setText("下一步")
        self.update_gate()

    def forward(self):
        if self.step < 2:
            self.step += 1
            self.pages.setCurrentIndex(self.step)
            self.previous.setText("上一步")
            self.next.setText("开始备份" if self.step == 2 else "下一步")
            self.final_summary.setText(f"{len(self.selected())} 条会话\n\n保存到：{self.destination.text()}\n\n恢复密钥：{self.key_path}")
            self.project_summary.setText("包含关联项目工作文件；不含 Git 元数据、依赖缓存和独立凭据。原项目文件仍保留在 Mac。" if self.include_projects.isChecked() else "本次只保存项目归属信息，不包含项目工作文件。需要时可返回上一步勾选。")
            self.update_gate()
            return
        selected = self.selected()
        self.owner.selected.update(r.uid for r in selected)
        self.owner.save_selection()
        self.accept()
        self.owner.run_backup(selected, Path(self.destination.text()), self.key_path, self.include_projects.isChecked())
