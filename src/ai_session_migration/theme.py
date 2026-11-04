STYLE = """
QWidget { background:#111316; color:#e9edf2; font-family:'.AppleSystemUIFont','Segoe UI','Microsoft YaHei'; font-size:13px; }
QMainWindow, QDialog { background:#111316; }
QLabel { background:transparent; }
QLabel#Heading { font-size:24px; font-weight:600; }
QLabel#Subheading { color:#9ca8b6; font-size:12px; }
QLabel#Muted { color:#81909f; font-size:11px; }
QLabel#Brand { font-size:16px; font-weight:600; }
QLabel#Section { font-size:14px; font-weight:600; }
QLabel#SidebarCaption { color:#81909f; font-size:10px; padding:12px 10px 5px; }
QLabel#Notice { background:#2b251a; color:#e9c17d; border:1px solid #58462d; border-radius:7px; padding:12px; font-size:12px; }
QLabel#Info { background:#202c40; color:#b3c8ee; border:1px solid #384e70; border-radius:7px; padding:12px; }
QLabel#Good { color:#b8e7d4; }
QLabel#Error { color:#ee9b96; }
QFrame#Sidebar { background:#101215; border-right:1px solid #2c3138; }
QFrame#Card { background:#191c20; border:1px solid #2c3138; border-radius:9px; }
QFrame#UserBubble { background:#262c34; border:1px solid #343d49; border-radius:9px; }
QFrame#Assistant { background:#191c20; border:0; }
QFrame#ToolCard { background:#1c2025; border:1px solid #303842; border-radius:7px; }
QPushButton { background:#20252b; border:1px solid #3a414b; border-radius:7px; padding:8px 13px; min-height:19px; }
QPushButton:hover { background:#2b323a; border-color:#536171; }
QPushButton:pressed { background:#354039; }
QPushButton:disabled { color:#68717c; background:#1b2025; border-color:#2c323a; }
QPushButton#Primary { color:#172e23; background:#b8e7d4; border:1px solid #b8e7d4; font-weight:600; }
QPushButton#Primary:hover { background:#ceefdf; }
QPushButton#Primary:disabled { background:#35483e; color:#8c9e92; border-color:#35483e; }
QPushButton#Nav { background:transparent; border:0; color:#a2adbb; text-align:left; padding:10px 13px; }
QPushButton#Nav:hover { background:#20262d; }
QPushButton#Nav:checked { background:#24382e; color:#c9ebd9; }
QPushButton#Quiet { background:transparent; border:0; color:#9aa8b8; }
QToolButton { background:transparent; border:0; color:#a3b2c1; padding:8px; text-align:left; }
QToolButton:hover { color:#d8e9df; }
QLineEdit, QComboBox { background:#171b20; border:1px solid #343c47; border-radius:7px; min-height:22px; padding:7px 11px; selection-background-color:#385846; }
QLineEdit:focus, QComboBox:focus { border-color:#86b59b; }
QComboBox::drop-down { border:0; width:23px; }
QComboBox QAbstractItemView { background:#222830; selection-background-color:#304639; border:1px solid #465548; }
QListWidget { background:#191c20; border:1px solid #2c3138; border-radius:8px; outline:0; }
QListWidget::item { border-bottom:1px solid #282e35; padding:9px; }
QListWidget::item:selected { background:#25382f; color:#e9edf2; }
QTreeWidget { background:transparent; border:0; outline:0; font-size:12px; }
QTreeWidget::item { padding:7px 3px; }
QTreeWidget::item:selected { background:#24382e; color:#c9ebd9; }
QHeaderView::section { background:#1d2228; color:#9eabba; border:0; padding:9px; }
QTextBrowser, QPlainTextEdit { background:transparent; color:#cbd4df; border:0; selection-background-color:#3c5c4b; font-size:13px; }
QTabWidget::pane { border:0; border-top:1px solid #2c3138; }
QTabBar::tab { color:#8493a3; padding:12px 17px; background:transparent; border-bottom:2px solid transparent; }
QTabBar::tab:selected { color:#c9e6d7; border-bottom:2px solid #b8e7d4; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { background:transparent; width:7px; margin:3px; }
QScrollBar::handle:vertical { background:#39444f; border-radius:3px; min-height:28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }
QSplitter::handle { background:#111316; width:12px; }
QProgressBar { border:0; background:#29352e; border-radius:4px; height:7px; text-align:center; color:transparent; }
QProgressBar::chunk { background:#b8e7d4; border-radius:4px; }
QCheckBox { spacing:8px; }
QCheckBox::indicator { width:14px; height:14px; border:1px solid #5a6878; border-radius:3px; background:#161a20; }
QCheckBox::indicator:checked { background:#b8e7d4; border-color:#b8e7d4; }
QStatusBar { background:#101215; color:#81909f; border-top:1px solid #2c3138; font-size:10px; }
QMessageBox { background:#191c20; }
QMenuBar, QMenu { background:#1b2026; color:#d9e2eb; }
QMenu::item:selected { background:#2d4938; }
"""
