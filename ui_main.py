# -*- coding: utf-8 -*-
"""ima 桌面客户端主界面（PySide6）。

四个标签页：知识库(OpenAPI 树+批量下载) / 对话(Cookie LLM) / 智能体(md整理+技能+记忆) / 设置(凭据)。
网络操作走 Worker 线程，UI 不卡顿。
"""
import os
import sys
import json
import io

from PySide6.QtCore import QThread, Signal, Qt, QFileSystemWatcher, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QFileDialog, QMessageBox, QProgressBar, QComboBox, QInputDialog, QSplitter,
    QPlainTextEdit, QListWidget, QListWidgetItem, QDialog, QAbstractItemView,
    QMenu, QDialogButtonBox, QFormLayout, QCheckBox,
)

import config
import ima_openapi
import ima_cookie
import agents


# ---------------- 通用后台 Worker ----------------
class Worker(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    result = Signal(object)
    error = Signal(str)
    chunk = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            res = self.fn(self, *self.args, **self.kwargs)
            self.result.emit(res)
        except Exception as e:
            self.error.emit(str(e))


# ---------------- 目标位置选择对话框 ----------------
class NoteDialog(QDialog):
    """新建 ima 笔记：标题 + Markdown 正文，可选同时加入某个知识库。

    走官方 OpenAPI 的笔记模块（openapi/note/v1/import_doc，实测可用），
    不需要 Cookie 通道，因此不受内部接口变更影响。
    """

    def __init__(self, parent, kbs):
        super().__init__(parent)
        self.setWindowTitle("新建 ima 笔记")
        self.resize(760, 560)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("笔记标题（成为正文的一级标题）")
        form.addRow("标题：", self.ed_title)
        lay.addLayout(form)

        lay.addWidget(QLabel("正文（Markdown，支持 # 标题、- 列表、**加粗**、``` 代码块）："))
        self.ed_body = QPlainTextEdit()
        self.ed_body.setPlaceholderText("直接写正文…")
        lay.addWidget(self.ed_body, 1)

        row = QHBoxLayout()
        self.chk_kb = QCheckBox("保存后同时加入知识库：")
        self.combo_kb = QComboBox()
        for k in kbs:
            self.combo_kb.addItem(k["name"], k["id"])
        if not kbs:
            self.combo_kb.addItem("（无可用知识库）", "")
        self.combo_kb.setEnabled(False)
        self.chk_kb.setEnabled(bool(kbs))
        self.chk_kb.toggled.connect(self.combo_kb.setEnabled)
        row.addWidget(self.chk_kb)
        row.addWidget(self.combo_kb, 1)
        lay.addLayout(row)

        tip = QLabel("说明：笔记先创建在 ima 笔记侧；勾选加入知识库后，会以 📝 条目出现在该库根目录，"
                     "之后可用右键「移动到…」调整到文件夹。")
        tip.setStyleSheet("color:#666;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存笔记")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def result_note(self):
        return (self.ed_title.text().strip(), self.ed_body.toPlainText(),
                self.chk_kb.isChecked(), self.combo_kb.currentData())


class NoteViewer(QDialog):
    """查看/导出 ima 笔记正文。"""

    def __init__(self, parent, title, text, save_dir=None):
        super().__init__(parent)
        self.setWindowTitle("笔记：" + (title or ""))
        self.resize(820, 600)
        self._title = title or "笔记"
        self._save_dir = save_dir
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText(text or "")
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        b_copy = QPushButton("复制全文")
        b_save = QPushButton("另存为 Markdown…")
        b_close = QPushButton("关闭")
        b_copy.clicked.connect(self._copy)
        b_save.clicked.connect(self._save)
        b_close.clicked.connect(self.accept)
        row.addWidget(b_copy)
        row.addWidget(b_save)
        row.addStretch(1)
        row.addWidget(b_close)
        lay.addLayout(row)

    def _copy(self):
        QApplication.clipboard().setText(self.view.toPlainText())
        QMessageBox.information(self, "已复制", "笔记全文已复制到剪贴板")

    def _save(self):
        import ima_openapi
        default = os.path.join(self._save_dir or "",
                               ima_openapi.sanitize_name(self._title) + ".md")
        fp, _ = QFileDialog.getSaveFileName(self, "另存为 Markdown", default,
                                            "Markdown (*.md);;所有文件 (*.*)")
        if not fp:
            return
        try:
            with io.open(fp, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText())
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e)[:200])
            return
        QMessageBox.information(self, "已保存", fp)


# ---------------- 本地知识库：目录选择与新建 ----------------
# 坑：Windows 的「选择文件夹」允许选中「此电脑 / 网络」等虚拟节点。
# 这类路径在磁盘上不存在，Qt 拿回来后再做任何文件操作，系统都会弹
# 「你不能使用该程序打开此位置。请尝试其他位置。」——建库必然失败。
# 因此这里统一做两层防护：① 过滤虚拟路径；② 建库改为「填名称 + 选父目录，程序自己建」。
_VIRTUAL_NAMES = ("此电脑", "我的电脑", "this pc", "computer", "网络", "network",
                  "回收站", "recycle bin", "控制面板", "control panel",
                  "库", "libraries", "快速访问", "quick access")


def is_virtual_path(path):
    """判断是否为 Windows 虚拟（非磁盘）路径。"""
    p = (path or "").strip().strip('"').strip()
    if not p:
        return True
    if p.startswith("::"):                      # shell 命名空间，如 ::{20D04FE0-...}
        return True
    low = p.replace("/", "\\").rstrip("\\").lower()
    for w in _VIRTUAL_NAMES:
        if low == w or low.endswith("\\" + w):
            return True
    return False


def default_lib_parent():
    """本地知识库的默认存放位置：优先「我的文档」，退回用户主目录。"""
    home = os.path.expanduser("~")
    for c in (os.path.join(home, "Documents"), home):
        if os.path.isdir(c):
            return c
    return getattr(config, "LOCAL_LIB_DIR", os.getcwd())


def safe_pick_dir(parent, title, start=""):
    """安全的目录选择：过滤虚拟路径，只返回真实存在的目录，否则返回 ""。"""
    s = (start or "").strip()
    if not s or is_virtual_path(s) or not os.path.isdir(s):
        s = default_lib_parent()
    d = QFileDialog.getExistingDirectory(
        parent, title, s,
        QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks)
    d = (d or "").strip().strip('"')
    if not d:
        return ""
    if is_virtual_path(d):
        QMessageBox.warning(
            parent, "这个位置不能用",
            "你选的是「%s」，它不是磁盘上的真实文件夹。\n\n"
            "请进入具体磁盘（例如 C:\\ 或 D:\\）后，再选择一个文件夹。"
            % d[:60])
        return ""
    d = os.path.abspath(d)
    if not os.path.isdir(d):
        QMessageBox.warning(parent, "文件夹不存在", "该路径不是有效文件夹：\n%s" % d)
        return ""
    return d


class LocalLibDialog(QDialog):
    """把本机上一个**已有**文件夹添加为本地知识库。

    客户端只读取该文件夹，不新建、不改名、不删除其中任何内容；
    也不会替用户创建目录（早期版本会自动建子文件夹，已按要求去掉）。
    """

    def __init__(self, parent=None, known=None):
        super().__init__(parent)
        self.setWindowTitle("选择本地知识库文件夹")
        self.setMinimumWidth(640)
        self.chosen = ""
        self._known = list(known or [])

        lay = QVBoxLayout(self)
        tip = QLabel("选择本机上一个已有文件夹作为本地知识库。\n"
                     "客户端只读取它，不会新建或改动里面的任何文件；"
                     "文件按原目录层级显示，可勾选后推送到 ima 知识库。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addWidget(QLabel("文件夹："))
        self.ed_loc = QLineEdit(default_lib_parent())
        self.ed_loc.setPlaceholderText(r"例如：D:\我的资料")
        b_browse = QPushButton("浏览…")
        b_browse.clicked.connect(self._browse)
        b_home = QPushButton("我的文档")
        b_home.clicked.connect(lambda: self.ed_loc.setText(default_lib_parent()))
        row.addWidget(self.ed_loc, 1)
        row.addWidget(b_browse)
        row.addWidget(b_home)
        lay.addLayout(row)

        self.lbl_path = QLabel()
        self.lbl_path.setWordWrap(True)
        self.lbl_path.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_path)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("添加")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.ed_loc.textChanged.connect(self._update_preview)
        self._update_preview()

    # --- 工具 ---
    def _path(self):
        return (self.ed_loc.text() or "").strip().strip('"')

    def _update_preview(self):
        p = self._path()
        if not p:
            self.lbl_path.setText("请选择一个已存在的文件夹")
        elif is_virtual_path(p):
            self.lbl_path.setText("⚠ 这不是磁盘上的真实文件夹，请重新选择")
        elif not os.path.isdir(p):
            self.lbl_path.setText("⚠ 该文件夹不存在：%s" % p)
        else:
            self.lbl_path.setText("将添加：" + os.path.abspath(p))

    def _browse(self):
        d = safe_pick_dir(self, "选择本地知识库文件夹",
                          self._path() or default_lib_parent())
        if d:
            self.ed_loc.setText(d)

    def _ok(self):
        p = self._path()
        if not p:
            QMessageBox.warning(self, "提示", "请选择或填写文件夹路径。")
            return
        if is_virtual_path(p):
            QMessageBox.warning(
                self, "这个位置不能用",
                "「%s」不是磁盘上的真实文件夹。\n\n"
                "请进入具体磁盘（例如 C:\\ 或 D:\\）后选择一个文件夹。" % p[:60])
            return
        p = os.path.abspath(p)
        if os.path.isfile(p):
            QMessageBox.warning(self, "这个位置不能用",
                                "该路径是一个文件，不是文件夹：\n%s" % p)
            return
        if not os.path.isdir(p):
            QMessageBox.warning(
                self, "文件夹不存在",
                "该文件夹不存在：\n%s\n\n"
                "本客户端不会替你创建目录，请先在资源管理器里建好，"
                "或另选一个已有文件夹。" % p)
            return
        if p in self._known:
            if QMessageBox.question(
                    self, "已在列表中",
                    "这个文件夹已经在本地知识库列表里：\n%s\n\n直接切换到它吗？" % p,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                return
        self.chosen = p
        self.accept()

# ---------------- 主窗口 ----------------
class MainWindow(QMainWindow):
    # 上下文强度 -> (记忆条数, 记忆字符上限, 本地资料字符上限)
    # ima 服务端的检索与上下文窗口客户端改不了，但「本地往问题里塞多少」可控。
    CTX_LEVELS = {
        "关闭": (0, 0, 0),
        "轻度": (8, 2500, 8000),
        "标准": (15, 5000, 30000),
        "强力": (40, 12000, 60000),
    }

    def __init__(self):
        super().__init__()
        self.cfg = config.get_config()
        self._workers = []
        self._worker = None
        self.setWindowTitle("ima 桌面客户端  (飞@_@ 自用)")
        self.resize(1100, 720)
        self._build_ui()
        # 启动后异步拉一次真实模型表：顺带完成 Cookie 票据续期并回写配置。
        # 否则界面用内置表看起来一切正常，底层票据已过期，直到首次操作才暴露。
        QTimer.singleShot(1200, lambda: self.reload_models(fetch=True))

    # ===== UI 构建 =====
    def _build_ui(self):
        tabs = QTabWidget()
        self.tab_kb = self._build_kb_tab()
        self.tab_chat = self._build_chat_tab()
        self.tab_agent = self._build_agent_tab()
        self.tab_local = self._build_local_tab()
        self.tab_settings = self._build_settings_tab()
        tabs.addTab(self.tab_kb, "知识库")
        tabs.addTab(self.tab_chat, "对话")
        tabs.addTab(self.tab_agent, "智能体")
        tabs.addTab(self.tab_local, "本地库")
        tabs.addTab(self.tab_settings, "设置")
        self.setCentralWidget(tabs)

    # ---------- 知识库 Tab ----------
    def _build_kb_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.btn_refresh_kb = QPushButton("刷新知识库列表")
        self.btn_refresh_kb.clicked.connect(self.refresh_kb_list)
        self.btn_check_all = QPushButton("全选")
        self.btn_check_all.clicked.connect(self.toggle_check_all)
        self.btn_download = QPushButton("下载勾选")
        self.btn_download.clicked.connect(self.download_selected)
        self.btn_upload = QPushButton("上传文件")
        self.btn_upload.clicked.connect(self.upload_files)
        self.btn_new_note = QPushButton("新建笔记")
        self.btn_new_note.clicked.connect(self.new_note)
        self.btn_choose_dir = QPushButton("选择下载目录")
        self.btn_choose_dir.clicked.connect(self.choose_download_dir)
        self.lbl_dir = QLabel(self.cfg.download_dir())
        top.addWidget(self.btn_refresh_kb)
        top.addWidget(self.btn_check_all)
        top.addWidget(self.btn_download)
        top.addWidget(self.btn_upload)
        top.addWidget(self.btn_new_note)
        top.addWidget(self.btn_choose_dir)
        top.addWidget(self.lbl_dir, 1)
        lay.addLayout(top)

        hint = QLabel("提示：下载勾选可批量下载（笔记自动导出为 .md）；右键笔记可查看正文；"
                      "本版本为只读浏览版，不支持重命名/删除/移动等修改操作")
        hint.setStyleSheet("color:#666;")
        lay.addWidget(hint)

        self.kb_tree = QTreeWidget()
        self.kb_tree.setHeaderLabel("知识库 / 文件夹 / 文件")
        self.kb_tree.itemExpanded.connect(self.on_tree_expand)
        self.kb_tree.itemClicked.connect(self.on_kb_item_clicked)
        self.kb_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.kb_tree.customContextMenuRequested.connect(self.on_kb_context_menu)
        self.kb_anchor = None
        self._kb_client = None          # 最近一次使用的 Cookie 客户端（供内部只读接口复用）
        self._oa_kid_map = {}           # 内部 kb_id -> OpenAPI kb_id
        lay.addWidget(self.kb_tree, 1)

        self.kb_log = QPlainTextEdit()
        self.kb_log.setReadOnly(True)
        self.kb_log.setMaximumHeight(160)
        lay.addWidget(self.kb_log)

        self.kb_bar = QProgressBar()
        self.kb_bar.setValue(0)
        lay.addWidget(self.kb_bar)
        return w

    def _openapi_client(self):
        cid, ckey = self.cfg.openapi()
        if not (cid and ckey):
            QMessageBox.warning(self, "提示", "请先在「设置」填写 OpenAPI clientid / apikey")
            return None
        return ima_openapi.OpenAPIClient(cid, ckey)

    def _sync_token(self, new_cookie, new_bkn):
        """Cookie 自动续期成功后，把新票据写回配置并落盘（下次启动免重抓）。"""
        try:
            self.cfg.set("cookie_xima_cookie", new_cookie)
            self.cfg.set("cookie_xima_bkn", new_bkn)
        except Exception:
            pass

    def _kbclient(self):
        """Cookie 客户端（内部知识库接口用）；未配置凭据返回 None。"""
        ck, bkn = self.cfg.cookie()
        if not (ck and bkn):
            return None
        return ima_cookie.ImaCookieClient(ck, bkn, self.cfg.get("default_kb_id"),
                                          on_token_refreshed=self._sync_token)

    def refresh_kb_list(self):
        cli = self._kbclient()
        if not cli:
            QMessageBox.warning(self, "提示",
                                "请先在「设置」粘贴 Cookie（x-ima-cookie / x-ima-bkn）")
            return
        self._kb_client = cli
        # OpenAPI 侧的名称->id 映射，供下载/上传使用（两套 id 不通用）
        oa_map = {}
        try:
            cid, ckey = self.cfg.openapi()
            if cid and ckey:
                for kid, name in ima_openapi.OpenAPIClient(cid, ckey).list_knowledge_bases():
                    oa_map.setdefault(name, kid)
        except Exception:
            pass

        self.kb_tree.clear()

        def job(w):
            w.log.emit("获取知识库列表（内部接口）...")
            return cli.kb_home()

        def on_result(kbs):
            if not isinstance(kbs, list):
                return
            self._oa_kid_map = {}
            # 自己的库（type 1/2）排在共享库（type 3）前面
            kbs = sorted(kbs, key=lambda k: (str(k.get("type")) == "3", k.get("name") or ""))
            n_own = 0
            for kb in kbs:
                iid, name, tp = kb["id"], kb["name"], str(kb.get("type"))
                okid = oa_map.get(name, "")
                self._oa_kid_map[iid] = okid
                if tp in ("1", "2"):
                    n_own += 1
                    suffix = "" if okid else "  （未加入 OpenAPI·不可上传下载）"
                else:
                    suffix = "  （共享库·只读）"
                item = QTreeWidgetItem(self.kb_tree, [name + suffix])
                item.setData(0, Qt.UserRole, ("kb", iid, None, okid))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            self.kb_log.appendPlainText(
                "共 %d 个知识库（自有/已加入 %d 个）；点箭头或双击展开" % (len(kbs), n_own))

        self._run(job, None, self.kb_bar, result_cb=on_result)

    def _fill_children(self, item, kb_id, okid, items):
        """在主线程把条目填进树节点。"""
        for it in items:
            title, mid = it.get("title", ""), it.get("media_id", "")
            if it.get("is_folder"):
                child = QTreeWidgetItem(item, ["📁 " + title])
                child.setData(0, Qt.UserRole, ("folder", kb_id, mid, okid))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
                child.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            else:
                # media_type 11 / media_id 前缀 note_ => ima 笔记，单独图标便于识别
                is_note = str(it.get("media_type") or "") == "11" or str(mid).startswith("note_")
                child = QTreeWidgetItem(item, [("📝 " if is_note else "📄 ") + title])
                child.setData(0, Qt.UserRole, ("file", kb_id, mid, okid))
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked)
        item.setExpanded(True)
        self.kb_log.appendPlainText("  %s -> %d 项" % (item.text(0), len(items)))

    def on_tree_expand(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind, kb_id, media_id, okid = data
        if kind == "file" or item.childCount() > 0:
            return
        cli = self._ensure_kb_client()
        if cli is None:
            return
        name = item.text(0)
        folder_id = media_id or ""

        def job(w):
            w.log.emit("展开: " + name)
            return cli.kb_list(kb_id, folder_id)

        self._run(job, None, self.kb_bar,
                  result_cb=lambda items: self._fill_children(item, kb_id, okid, items or []))

    def _flatten_tree(self):
        """按显示顺序展开所有节点，用于 Shift 连续勾选。"""
        out = []

        def walk(parent):
            for i in range(parent.childCount()):
                c = parent.child(i)
                out.append(c)
                walk(c)

        for i in range(self.kb_tree.topLevelItemCount()):
            t = self.kb_tree.topLevelItem(i)
            out.append(t)
            walk(t)
        return out

    def _apply_check_range(self, anchor, item):
        """把 anchor 到 item 之间的所有节点，按 anchor 的勾选状态批量设置。"""
        flat = self._flatten_tree()
        try:
            i1 = flat.index(anchor)
            i2 = flat.index(item)
        except ValueError:
            return
        if i1 > i2:
            i1, i2 = i2, i1
        state = anchor.checkState(0)
        for it in flat[i1:i2 + 1]:
            it.setCheckState(0, state)

    def on_kb_item_clicked(self, item, col):
        """Shift+点击：把锚点到当前节点的区间批量勾选/取消（跟随锚点状态），便于快速多选。"""
        mods = QApplication.keyboardModifiers()
        if (mods & Qt.ShiftModifier) and self.kb_anchor is not None and self.kb_anchor is not item:
            self._apply_check_range(self.kb_anchor, item)
        else:
            # 普通点击：锚点更新为当前节点（复选框由 Qt 自动切换）
            self.kb_anchor = item

    def download_selected(self):
        # 收集所有勾选的节点
        checked = []

        def collect(it):
            if it.checkState(0) == Qt.Checked:
                checked.append(it)
            for i in range(it.childCount()):
                collect(it.child(i))

        for i in range(self.kb_tree.topLevelItemCount()):
            collect(self.kb_tree.topLevelItem(i))

        targets = [(it.text(0), it.data(0, Qt.UserRole)) for it in checked
                   if it.data(0, Qt.UserRole) and it.data(0, Qt.UserRole)[0] in ("kb", "folder", "file")]
        if not targets:
            QMessageBox.information(self, "提示", "请先勾选要下载的知识库/文件夹/文件（可多选）")
            return
        cli = self._openapi_client()
        if not cli:
            return
        has_dir = any(k in ("kb", "folder") for _, (k, _, _, _) in targets)
        recursive = True
        if has_dir:
            recursive = QMessageBox.question(
                self, "递归下载", "是否递归下载所有子文件夹？",
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

        base = self.cfg.download_dir()

        def dl_file(w, kb_id, media_id, name, d):
            # 笔记类条目没有文件直链，改为导出 Markdown 正文
            if str(media_id).startswith("note_"):
                nid = ima_openapi.note_id_from_media(media_id)
                try:
                    md = cli.get_note_content(nid, 1)
                except Exception as e:
                    w.log.emit("  [FAIL] %s (笔记读取失败: %s)" % (name, str(e)[:80]))
                    return
                fp = os.path.join(d, ima_openapi.sanitize_name(name) + ".md")
                with io.open(fp, "w", encoding="utf-8") as f:
                    f.write(md)
                w.log.emit("  [OK·笔记导出] %s (%d 字)" % (name, len(md)))
                return
            mi = cli.get_media_info(media_id)
            url, hdrs = cli.build_download_url(mi)
            if not url:
                w.log.emit("  [SKIP] %s (无下载链接)" % name)
                return
            fp = os.path.join(d, ima_openapi.sanitize_name(name))
            sz = cli.download_file(url, hdrs, fp)
            w.log.emit("  [OK] %s (%d bytes)" % (name, sz))

        def job(w):
            t0 = __import__("time").time()
            total_ok = 0
            for idx, (name, (kind, kb_id, node_id, okid)) in enumerate(targets, 1):
                w.log.emit("=== [%d/%d] %s ===" % (idx, len(targets), name))
                try:
                    if kind == "file":
                        dl_file(w, kb_id, node_id, name, base)
                        total_ok += 1
                    else:
                        if not okid:
                            w.log.emit("  [SKIP] 该知识库不在 OpenAPI 可见范围内，无法下载")
                            continue
                        out_dir = os.path.join(base, ima_openapi.sanitize_name(name))
                        d, t = cli.download_tree(okid, node_id, out_dir,
                                                 recursive=recursive,
                                                 on_log=lambda m: w.log.emit("  " + m),
                                                 on_progress=lambda dd, tt: w.progress.emit(dd, tt))
                        total_ok += d
                except Exception as e:
                    w.log.emit("  [FAIL] %s: %s" % (name, e))
            w.log.emit("全部完成：%d 个文件，用时 %.1fs -> %s"
                       % (total_ok, __import__("time").time() - t0, base))

        self._run(job, self.kb_log.appendPlainText, self.kb_bar)

    def toggle_check_all(self):
        state = Qt.Checked if self.btn_check_all.text() == "全选" else Qt.Unchecked

        def walk(it):
            it.setCheckState(0, state)
            for i in range(it.childCount()):
                walk(it.child(i))

        for i in range(self.kb_tree.topLevelItemCount()):
            walk(self.kb_tree.topLevelItem(i))
        self.btn_check_all.setText("全不选" if state == Qt.Checked else "全选")

    def choose_download_dir(self):
        d = safe_pick_dir(self, "选择下载目录", self.cfg.download_dir())
        if d:
            self.cfg.set_download_dir(d)
            self.lbl_dir.setText(d)

    def upload_files(self):
        item = self.kb_tree.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先在左侧选中一个知识库（或具体文件夹）作为上传目标")
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        kind, kb_id, folder_id, okid = data
        if kind == "file":
            # 文件节点上溯到所属文件夹
            parent = item.parent()
            if parent:
                pd = parent.data(0, Qt.UserRole)
                kind, kb_id, folder_id, okid = pd
        if kind not in ("kb", "folder"):
            QMessageBox.information(self, "提示", "请选中知识库或文件夹节点")
            return
        if not okid:
            QMessageBox.warning(self, "无法上传",
                                "该知识库不在 OpenAPI 可见范围内（未加入你的空间），无法上传。")
            return
        target = item.text(0)
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件 -> 「%s」" % target, "",
            "常用文档 (*.pdf *.md *.txt *.doc *.docx *.xls *.xlsx *.ppt *.pptx);;所有文件 (*.*)")
        if not files:
            return
        cli = self._openapi_client()
        if not cli:
            return

        def job(w):
            ok = 0
            for fp in files:
                w.log.emit("上传: " + os.path.basename(fp))
                res, msg = cli.upload_file(okid, folder_id, fp,
                                           on_log=lambda m: w.log.emit("  " + m))
                if res:
                    ok += 1
                    w.log.emit("  ✅ " + msg)
                else:
                    w.log.emit("  ❌ " + msg)
            w.log.emit("上传完成：%d/%d 成功，刷新列表查看" % (ok, len(files)))

        self._run(job, self.kb_log.appendPlainText, self.kb_bar)

    # ================= 知识库文件操作（右键菜单） =================
    @staticmethod
    def _clean_name(item):
        """去掉树节点的图标前缀，得到真实文件名。"""
        t = item.text(0)
        for pre in ("📁 ", "📄 ", "📝 "):
            if t.startswith(pre):
                return t[len(pre):]
        return t

    def _kb_checked_nodes(self):
        """收集勾选的节点 -> [(item, kind, kb_id, media_id, okid)]。

        父节点被勾选时不再返回其子孙（服务端移动/删除文件夹会连带内容，
        重复提交会互相干扰）。
        """
        out = []

        def walk(it):
            d = it.data(0, Qt.UserRole)
            if it.checkState(0) == Qt.Checked and d and d[0] in ("folder", "file"):
                out.append((it, d[0], d[1], d[2], d[3]))
                return
            for i in range(it.childCount()):
                walk(it.child(i))

        for i in range(self.kb_tree.topLevelItemCount()):
            walk(self.kb_tree.topLevelItem(i))
        return out

    def _kb_src_folder_id(self, item):
        """节点所在的父文件夹 media_id（根目录返回 ""）。"""
        p = item.parent()
        if not p:
            return ""
        pd = p.data(0, Qt.UserRole)
        return (pd[2] or "") if pd and pd[0] == "folder" else ""

    def _find_kb_item(self, media_id):
        """按 media_id 在树中找节点（用于判断目标是否位于源文件夹内部）。"""
        hit = []

        def walk(it):
            d = it.data(0, Qt.UserRole)
            if d and d[2] == media_id:
                hit.append(it)
                return
            for i in range(it.childCount()):
                walk(it.child(i))

        for i in range(self.kb_tree.topLevelItemCount()):
            walk(self.kb_tree.topLevelItem(i))
        return hit[0] if hit else None

    def _current_kb_item(self):
        it = self.kb_tree.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "请先选中一个节点")
            return None
        data = it.data(0, Qt.UserRole)
        if not data:
            return None
        return it, data

    def _reload_item(self, item):
        """清空并重新展开某节点（在主线程调用）。"""
        if not item:
            return
        d = item.data(0, Qt.UserRole)
        item.takeChildren()
        if item.isExpanded() or (d and d[0] == "kb"):
            item.setExpanded(True)
            self.on_tree_expand(item)

    def on_kb_context_menu(self, pos):
        item = self.kb_tree.itemAt(pos)
        if item:
            self.kb_tree.setCurrentItem(item)
        menu = QMenu(self)
        act_view_note = menu.addAction("查看笔记正文")
        menu.addSeparator()
        act_upload = menu.addAction("上传文件到此处")
        act_refresh = menu.addAction("刷新此节点")

        data = item.data(0, Qt.UserRole) if item else None
        kind = data[0] if data else None
        act_view_note.setEnabled(bool(kind == "file" and str(data[2]).startswith("note_")))
        act_upload.setEnabled(kind in ("kb", "folder"))

        act = menu.exec(self.kb_tree.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act == act_view_note:
            self.open_note_viewer()
        elif act == act_upload:
            self.upload_files()
        elif act == act_refresh:
            if item:
                self._reload_item(item)

    def _ensure_kb_client(self):
        cli = self._kb_client
        if cli is None:
            cli = self._kbclient()
            if cli is None:
                QMessageBox.warning(self, "提示",
                                    "请先在「设置」粘贴 Cookie（x-ima-cookie / x-ima-bkn）")
                return None
            self._kb_client = cli
        return cli

    # ================= 笔记（官方 OpenAPI note/v1，2026-09-20 实测可用） =================
    def new_note(self):
        """新建 ima 笔记，可选同时加入某个知识库。

        走 OpenAPI（而非 Cookie 内部接口），因此不受 Cookie 过期与前端改动影响。
        """
        cli = self._openapi_client()
        if not cli:
            return
        try:
            kbs = [{"id": i, "name": n} for i, n in cli.list_knowledge_bases()]
        except Exception as e:
            kbs = []
            self.kb_log.appendPlainText(
                "读取知识库列表失败（仍可只创建笔记）：" + str(e)[:140])
        dlg = NoteDialog(self, kbs)
        if dlg.exec() != QDialog.Accepted:
            return
        title, body, add_kb, kb_id = dlg.result_note()
        if not title and not body.strip():
            QMessageBox.information(self, "提示", "标题和正文至少填一项")
            return
        md = (("# %s\n\n" % title) if title else "") + body.strip()
        add_kb = bool(add_kb and kb_id)

        def job(w):
            w.log.emit("创建笔记「%s」（%d 字）…" % (title or "(无标题)", len(md)))
            nid, err = cli.create_note(md)
            if err:
                w.error.emit(err)
                return None
            w.log.emit("  ✅ 笔记已创建：note_id=%s" % nid)
            if add_kb:
                mid, err2 = cli.add_note_to_kb(kb_id, nid, title or "无标题笔记")
                if err2:
                    w.log.emit("  ⚠️ " + err2 + "（笔记本身已创建成功，可稍后手动加入）")
                else:
                    w.log.emit("  ✅ 已加入知识库：%s；点「刷新知识库列表」即可看到 📝 条目" % mid)
            w.log.emit("可在浏览器查看：https://ima.qq.com/note?note_id=%s" % nid)
            return None

        self._run(job, self.kb_log.appendPlainText, self.kb_bar)

    def open_note_viewer(self):
        """查看选中笔记的正文，可复制或另存为 Markdown。"""
        r = self._current_kb_item()
        if not r:
            return
        item, (kind, kb_id, media_id, okid) = r
        if kind != "file" or not str(media_id).startswith("note_"):
            QMessageBox.information(self, "提示", "请选中一条 📝 笔记条目（笔记以 note_ 开头的 media_id 标识）")
            return
        nid = ima_openapi.note_id_from_media(media_id)
        if not nid:
            QMessageBox.information(self, "提示", "无法解析 note_id：%s" % media_id)
            return
        cli = self._openapi_client()
        if not cli:
            return
        title = self._clean_name(item)

        def job(w):
            w.log.emit("读取笔记正文：%s" % title)
            return cli.get_note_content(nid, 1)

        def show(md):
            if md is None:
                return
            NoteViewer(self, title, md, self.cfg.download_dir()).exec()

        self._run(job, self.kb_log.appendPlainText, self.kb_bar, result_cb=show)

    def _build_chat_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        # 第一行：知识库 + 模型选择
        top = QHBoxLayout()
        top.addWidget(QLabel("知识库(可选):"))
        self.chat_kb = QLineEdit()
        self.chat_kb.setPlaceholderText("留空=自由对话；填内部ID=基于知识库回答")
        self.chat_kb.setText(self.cfg.get("default_kb_id"))
        top.addWidget(self.chat_kb, 1)
        top.addWidget(QLabel("模型:"))
        self.chat_model = QComboBox()
        self.chat_model.setMinimumWidth(230)
        self.chat_model.currentIndexChanged.connect(lambda _=0: self._save_current_model())
        top.addWidget(self.chat_model)
        self.btn_reload_models = QPushButton("刷新模型")
        self.btn_reload_models.clicked.connect(lambda: self.reload_models(fetch=True))
        top.addWidget(self.btn_reload_models)
        lay.addLayout(top)

        # 第二行：记忆文件 + 本地资料
        ctx = QHBoxLayout()
        ctx.addWidget(QLabel("记忆文件:"))
        self.chat_memfile = QComboBox()
        self.chat_memfile.setMinimumWidth(170)
        self.chat_memfile.currentTextChanged.connect(self._on_memfile_changed)
        ctx.addWidget(self.chat_memfile)
        self.lbl_memfile = QLabel("")
        ctx.addWidget(self.lbl_memfile)
        self.btn_load_local = QPushButton("加载本地资料")
        self.btn_load_local.clicked.connect(self.load_local_context)
        self.btn_clear_local = QPushButton("清除资料")
        self.btn_clear_local.clicked.connect(self.clear_local_context)
        ctx.addWidget(self.btn_load_local)
        ctx.addWidget(self.btn_clear_local)
        ctx.addStretch(1)
        # 上下文强度：控制客户端每次提问往 question 里塞多少记忆/资料
        # （服务端的上下文窗口改不了，但「我这边塞多少」可以调）
        ctx.addWidget(QLabel("上下文强度:"))
        self.ctx_level = QComboBox()
        self.ctx_level.addItems(list(self.CTX_LEVELS.keys()))
        lv = self.cfg.get("ctx_level") or "标准"
        self.ctx_level.setCurrentText(lv if lv in self.CTX_LEVELS else "标准")
        self.ctx_level.setToolTip("关闭=只发问题本身；强力=多带记忆与资料（长文档更慢，"
                                  "但能显著缓解「聊着聊着失忆」）")
        self.ctx_level.currentTextChanged.connect(
            lambda t: (self.cfg.set("ctx_level", t), self._update_local_label()))
        ctx.addWidget(self.ctx_level)
        self.btn_new_session = QPushButton("新会话")
        self.btn_new_session.setToolTip("丢弃服务端当前会话上下文，开始全新对话（换话题时用）")
        self.btn_new_session.clicked.connect(self.new_chat_session)
        ctx.addWidget(self.btn_new_session)
        lay.addLayout(ctx)
        self.lbl_local = QLabel("未加载本地资料（可加载 md/txt 作为对话上下文）")
        lay.addWidget(self.lbl_local)

        self.chat_view = QTextEdit()
        self.chat_view.setReadOnly(True)
        lay.addWidget(self.chat_view, 1)

        bottom = QHBoxLayout()
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("输入消息，回车发送（记忆文件会自动载入上下文、问答会自动存入）")
        self.chat_input.returnPressed.connect(self.send_chat)
        self.btn_send = QPushButton("发送")
        self.btn_send.clicked.connect(self.send_chat)
        bottom.addWidget(self.chat_input, 1)
        bottom.addWidget(self.btn_send)
        lay.addLayout(bottom)

        self.local_files = []          # [(path, name)]
        self.mem_files = agents.MemoryFiles()
        self.mem_files.ensure_default()
        self._models_loaded = False
        self.reload_models()
        self.reload_memfiles()
        return w

    # ---- 模型选择 ----
    def reload_models(self, fetch=False):
        """fetch=True 时用 Cookie 拉官方模型表（后台线程）；否则用内置表。"""
        if not fetch:
            self._apply_models(ima_cookie.FALLBACK_MODELS)
            return
        ck, bkn = self.cfg.cookie()
        if not (ck and bkn):
            self._apply_models(ima_cookie.FALLBACK_MODELS)
            return

        def job(w):
            try:
                ms = ima_cookie.ImaCookieClient(
                    ck, bkn, on_token_refreshed=self._sync_token).get_models()
            except Exception as e:
                ms = e          # 交给 _apply_models 提示（不再静默降级）
            w.result.emit(ms)

        self._run(job, None, None, result_cb=self._apply_models)

    def _apply_models(self, models):
        if isinstance(models, Exception):
            try:
                self.kb_log.appendPlainText("模型列表获取失败：%s" % models)
            except Exception:
                pass
            models = ima_cookie.FALLBACK_MODELS
        if not models:
            models = ima_cookie.FALLBACK_MODELS
        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        mid, mtype, _lbl = self.cfg.model()
        cur = -1
        for m in models:
            for (sub_label, sid, stype) in m["sub_models"]:
                self.chat_model.addItem("%s / %s" % (m["model_name"], sub_label),
                                        (sid, stype))
                if sid == mid and stype == mtype:
                    cur = self.chat_model.count() - 1
        if cur < 0 and self.chat_model.count():
            cur = 0
        if cur >= 0:
            self.chat_model.setCurrentIndex(cur)
        self.chat_model.blockSignals(False)
        self._save_current_model()

    def _save_current_model(self):
        if not hasattr(self, "chat_model"):
            return
        data = self.chat_model.currentData()
        if data:
            self.cfg.set_model(data[0], data[1], self.chat_model.currentText())

    # ---- 记忆文件 ----
    def reload_memfiles(self):
        self.mem_files.ensure_default()
        self.chat_memfile.blockSignals(True)
        self.chat_memfile.clear()
        files = self.mem_files.list_files()
        for f in files:
            self.chat_memfile.addItem(f)
        cur = self.cfg.memory_file()
        if cur in files:
            self.chat_memfile.setCurrentText(cur)
        elif files:
            self.chat_memfile.setCurrentIndex(0)
        self.chat_memfile.blockSignals(False)
        self._on_memfile_changed(self.chat_memfile.currentText())

    def _on_memfile_changed(self, name):
        if not name:
            return
        self.cfg.set_memory_file(name)
        n = len(self.mem_files.entries(name))
        self.lbl_memfile.setText("(当前 %d 条，自动载入/记录)" % n)

    def load_local_context(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择本地资料（md/txt，可多选）", "",
            "Markdown/文本 (*.md *.txt);;所有文件 (*.*)")
        if not files:
            return
        for fp in files:
            if fp not in [p for p, _ in self.local_files]:
                self.local_files.append((fp, os.path.basename(fp)))
        self._update_local_label()

    def clear_local_context(self):
        self.local_files = []
        self._update_local_label()

    def _update_local_label(self):
        if self.local_files:
            names = "、".join(n for _, n in self.local_files)
            base = "已加载 %d 个本地资料: %s" % (len(self.local_files), names)
        else:
            base = "未加载本地资料（可加载 md/txt 作为对话上下文）"
        lv = self.ctx_level.currentText() if hasattr(self, "ctx_level") else "标准"
        mem_n, mem_cap, loc_cap = self.CTX_LEVELS.get(lv, self.CTX_LEVELS["标准"])
        if mem_n <= 0 and loc_cap <= 0:
            base += "｜上下文强度：关闭（每次只发问题本身）"
        else:
            base += "｜上下文强度：%s（记忆≤%d条/≤%d字，资料≤%d字）" % (lv, mem_n, mem_cap, loc_cap)
        self.lbl_local.setText(base)

    def _build_agent_context(self):
        """拼接 记忆文件 + 本地资料，作为对话上下文（条数与字符上限按强度档位）。

        注意：这是**客户端侧**的补偿手段。ima 服务端自己的检索范围、上下文窗口、
        提示词规则都由腾讯控制，客户端改不了；能改的只是「每次提问时本地塞多少」。
        """
        lv = self.ctx_level.currentText() if hasattr(self, "ctx_level") else "标准"
        mem_n, mem_cap, loc_cap = self.CTX_LEVELS.get(lv, self.CTX_LEVELS["标准"])
        if mem_n <= 0 and loc_cap <= 0:
            return ""
        parts = []
        # 记忆文件（当前选中）
        name = self.chat_memfile.currentText().strip() if hasattr(self, "chat_memfile") else ""
        if name and mem_n > 0:
            mtxt = self.mem_files.recent_text(name, limit=mem_n)
            if mtxt:
                if len(mtxt) > mem_cap:
                    mtxt = "…（更早内容已省略）\n" + mtxt[-mem_cap:]
                parts.append("【智能体记忆（%s，最近 %d 条）】\n%s" % (name, mem_n, mtxt))
        # 本地资料
        if self.local_files and loc_cap > 0:
            buf, total, cap = [], 0, loc_cap
            for fp, nm in self.local_files:
                try:
                    c = io.open(fp, encoding="utf-8", errors="ignore").read()
                except Exception as e:
                    buf.append("（文件 %s 读取失败：%s）" % (nm, e))
                    continue
                if total + len(c) > cap:
                    c = c[: max(0, cap - total)]
                    buf.append("==== 本地资料：%s（已截断）====\n%s" % (nm, c))
                    break
                buf.append("==== 本地资料：%s ====\n%s" % (nm, c))
                total += len(c)
            if buf:
                parts.append("【本地资料】\n" + "\n\n".join(buf))
        if not parts:
            return ""
        return ("请参考以下上下文回答用户问题（优先依据本地资料与记忆）：\n"
                + "\n\n".join(parts) + "\n\n====================")

    def _cookie_client(self, fresh=False):
        """取对话用客户端；默认复用同一实例。

        重要：ima 的多轮上下文由服务端按 session_id 维护，客户端只负责复用同一个
        session。每次提问都新建客户端 = 每次都 init_session = 服务端拿不到上文，
        表现就是「聊着聊着就失忆」。因此这里缓存客户端，只有模型/知识库变更或
        用户点「新会话」时才重建。
        """
        ck, bkn = self.cfg.cookie()
        if not (ck and bkn):
            QMessageBox.warning(self, "提示", "请先在「设置」粘贴 Cookie (x-ima-cookie / x-ima-bkn)")
            return None
        data = self.chat_model.currentData() if self.chat_model.count() else None
        mid, mtype = data if data else ("official_3", 3)
        kb = self.chat_kb.text().strip()
        key = (mid, mtype, kb)
        c = getattr(self, "_chat_client", None)
        if fresh or c is None or getattr(self, "_chat_client_key", None) != key:
            c = ima_cookie.ImaCookieClient(ck, bkn, kb, model_id=mid, model_type=mtype,
                                           on_token_refreshed=self._sync_token)
            self._chat_client = c
            self._chat_client_key = key
        return c

    def new_chat_session(self):
        """开启新会话：丢掉当前 session，让服务端从零开始（旧话题不再干扰）。"""
        self._chat_client = None
        self._chat_client_key = None
        self.chat_view.append("<i style='color:#888'>—— 已开启新会话（服务端上下文已重置）——</i>")

    def send_chat(self):
        text = self.chat_input.text().strip()
        if not text:
            return
        cli = self._cookie_client()
        if not cli:
            return
        self._save_current_model()
        self.chat_view.append("<b>你：</b>" + text.replace("<", "&lt;"))
        self.chat_view.append("<b>ima：</b>")
        self.chat_input.clear()

        context = self._build_agent_context()
        q = (context + "\n\n用户问题：" + text) if context else text

        memname = self.chat_memfile.currentText().strip()
        if memname:
            self.mem_files.append(memname, text, role="user")   # 提问自动存记忆

        acc = {"t": ""}

        def on_chunk(t):
            acc["t"] += t
            self.chat_view.insertPlainText(t)
            self.chat_view.verticalScrollBar().setValue(
                self.chat_view.verticalScrollBar().maximum())

        def job(w):
            def cb(t):
                w.chunk.emit(t)
            cli.ask(q, on_chunk=cb)
            # 回答自动存记忆
            ans = acc["t"].strip()
            if ans and memname:
                self.mem_files.append(memname, ans, role="assistant")
                w.log.emit("__mem__")

        def on_log(m):
            if m == "__mem__":
                self._on_memfile_changed(memname)

        self._run(job, on_log, None, chunk_cb=on_chunk)

    # ---------- 智能体 Tab ----------
    def _build_agent_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.btn_organize = QPushButton("整理 md 文件")
        self.btn_organize.clicked.connect(self.organize_md)
        self.btn_reload_skills = QPushButton("刷新技能列表")
        self.btn_reload_skills.clicked.connect(self.reload_skills)
        self.btn_new_skill = QPushButton("新建技能")
        self.btn_new_skill.clicked.connect(self.new_skill)
        top.addWidget(self.btn_organize)
        top.addWidget(self.btn_reload_skills)
        top.addWidget(self.btn_new_skill)
        lay.addLayout(top)

        sp = QSplitter(Qt.Horizontal)
        self.skill_list = QTreeWidget()
        self.skill_list.setHeaderLabel("技能（双击删除）")
        self.skill_list.itemDoubleClicked.connect(self.delete_skill)
        self.agent_out = QPlainTextEdit()
        self.agent_out.setReadOnly(True)
        sp.addWidget(self.skill_list)
        sp.addWidget(self.agent_out)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 3)
        lay.addWidget(sp, 1)

        self.btn_mem = QPushButton("记忆文件管理")
        self.btn_mem.clicked.connect(self.open_memory_manager)
        lay.addWidget(self.btn_mem)
        self.reload_skills()
        return w

    def reload_skills(self):
        self.skill_list.clear()
        for s in agents.SkillsManager().list():
            QTreeWidgetItem(self.skill_list, [s.get("name", "?"),
                                             s.get("_file", "")])

    def new_skill(self):
        name, ok = QInputDialog.getText(self, "新建技能", "技能名称：")
        if not ok or not name:
            return
        desc, ok = QInputDialog.getText(self, "新建技能", "技能描述：")
        if not ok:
            return
        prompt, ok = QInputDialog.getMultiLineText(self, "新建技能", "技能提示词(prompt)：")
        if not ok:
            return
        p, e = agents.SkillsManager().save(name, desc, prompt)
        if e:
            QMessageBox.warning(self, "失败", e)
        else:
            self.reload_skills()
            QMessageBox.information(self, "成功", "已保存: " + os.path.basename(p))

    def delete_skill(self, item):
        fn = item.text(1)
        if QMessageBox.question(self, "删除", "删除技能 %s？" % fn,
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            agents.SkillsManager().delete(fn)
            self.reload_skills()

    def organize_md(self):
        cli = self._cookie_client()
        if not cli:
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择 Markdown 文件", "",
                                             "Markdown (*.md *.txt)")
        if not path:
            return
        # 选择技能（可选）
        skills = agents.SkillsManager().list()
        skill = None
        if skills:
            names = ["（不使用技能）"] + [s.get("name", "?") for s in skills]
            idx = QInputDialog.getItem(self, "选择技能", "应用技能：", names, 0, False)
            if idx[1] and idx[0] != "（不使用技能）":
                skill = next((s for s in skills if s.get("name") == idx[0]), None)

        agent = agents.Agent(llm=lambda q: cli.ask(q))
        self.agent_out.clear()
        self.agent_out.appendPlainText("正在整理：%s\n" % path)

        def job(w):
            def on_chunk(t):
                w.chunk.emit(t)
            # 替换 llm 以转发分片
            agent.llm = lambda q: self._ask_with_chunk(cli, q, on_chunk)
            res, err = agent.organize_markdown(path, skill=skill)
            if err:
                w.error.emit(err)
            else:
                w.result.emit(res)

        def on_chunk(t):
            self.agent_out.insertPlainText(t)

        def on_result(res):
            # 保存到同目录
            out = path + ".organized.md"
            with open(out, "w", encoding="utf-8") as f:
                f.write(res)
            self.agent_out.appendPlainText("\n\n--- 已保存到: " + out)

        self._run(job, lambda m: None, None, chunk_cb=on_chunk, result_cb=on_result)

    @staticmethod
    def _ask_with_chunk(cli, q, on_chunk):
        return cli.ask(q, on_chunk=on_chunk)

    # ---------- 记忆文件管理窗口 ----------
    def open_memory_manager(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("记忆文件管理")
        dlg.resize(760, 480)
        lay = QVBoxLayout(dlg)

        btns = QHBoxLayout()
        b_new = QPushButton("新建记忆文件")
        b_imp = QPushButton("上传(导入)记忆文件")
        b_del = QPushButton("删除该文件")
        b_cur = QPushButton("设为当前使用")
        btns.addWidget(b_new)
        btns.addWidget(b_imp)
        btns.addWidget(b_del)
        btns.addWidget(b_cur)
        btns.addStretch(1)
        lay.addLayout(btns)

        sp = QSplitter(Qt.Horizontal)
        self.mm_files = QListWidget()
        self.mm_files.setMinimumWidth(200)
        self.mm_entries = QListWidget()
        sp.addWidget(self.mm_files)
        sp.addWidget(self.mm_entries)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 2)
        lay.addWidget(sp, 1)

        ebtns = QHBoxLayout()
        b_del_e = QPushButton("删除选中的记忆")
        b_ref = QPushButton("刷新")
        ebtns.addWidget(b_del_e)
        ebtns.addWidget(b_ref)
        ebtns.addStretch(1)
        lay.addLayout(ebtns)

        def refresh_files():
            self.mm_files.clear()
            cur = self.cfg.memory_file()
            for fn in self.mem_files.list_files():
                it = QListWidgetItem(fn + ("  ← 当前" if fn == cur else ""))
                it.setData(Qt.UserRole, fn)
                self.mm_files.addItem(it)
            if self.mm_files.count():
                self.mm_files.setCurrentRow(0)

        def refresh_entries():
            self.mm_entries.clear()
            it = self.mm_files.currentItem()
            if not it:
                return
            for i, e in enumerate(self.mem_files.entries(it.data(Qt.UserRole))):
                li = QListWidgetItem("[%s] %s：%s" % (e["ts"], e["role"], e["content"]))
                li.setData(Qt.UserRole, i)
                self.mm_entries.addItem(li)

        def on_sel():
            refresh_entries()

        def do_new():
            name, ok = QInputDialog.getText(dlg, "新建记忆文件", "名称（自动加 .md）：")
            if ok and name:
                n, err = self.mem_files.create(name)
                if err:
                    QMessageBox.warning(dlg, "失败", err)
                refresh_files()

        def do_import():
            files, _ = QFileDialog.getOpenFileNames(
                dlg, "选择要导入为记忆的文件", "",
                "Markdown/文本 (*.md *.txt);;所有文件 (*.*)")
            if not files:
                return
            ok = 0
            for fp in files:
                n, err = self.mem_files.import_file(fp)
                if not err:
                    ok += 1
            refresh_files()
            QMessageBox.information(dlg, "导入完成", "成功导入 %d 个记忆文件" % ok)

        def do_del_file():
            it = self.mm_files.currentItem()
            if not it:
                return
            fn = it.data(Qt.UserRole)
            if QMessageBox.question(dlg, "删除", "删除记忆文件 %s？不可恢复。" % fn,
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            self.mem_files.delete(fn)
            if self.cfg.memory_file() == fn:
                self.cfg.set_memory_file("")
                self.mem_files.ensure_default()
            refresh_files()
            self.reload_memfiles()

        def do_set_current():
            it = self.mm_files.currentItem()
            if not it:
                return
            self.cfg.set_memory_file(it.data(Qt.UserRole))
            refresh_files()
            self.reload_memfiles()

        def do_del_entry():
            fit = self.mm_files.currentItem()
            eit = self.mm_entries.currentItem()
            if not fit or not eit:
                return
            self.mem_files.delete_entry(fit.data(Qt.UserRole), eit.data(Qt.UserRole))
            refresh_entries()
            self.reload_memfiles()

        self.mm_files.currentRowChanged.connect(lambda _=0: on_sel())
        b_new.clicked.connect(do_new)
        b_imp.clicked.connect(do_import)
        b_del.clicked.connect(do_del_file)
        b_cur.clicked.connect(do_set_current)
        b_del_e.clicked.connect(do_del_entry)
        b_ref.clicked.connect(refresh_files)

        refresh_files()
        dlg.exec()

    # ---------- 本地知识库 Tab ----------
    def _build_local_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.btn_new_lib = QPushButton("选择本地文件夹")
        self.btn_new_lib.setToolTip("把本机上一个已有文件夹添加为本地知识库（只读取，不新建/不改动任何文件）")
        self.btn_new_lib.clicked.connect(self.add_local_lib)
        self.btn_del_lib = QPushButton("移除所选库")
        self.btn_del_lib.clicked.connect(self.del_local_lib)
        self.btn_refresh_lib = QPushButton("刷新")
        self.btn_refresh_lib.clicked.connect(self.refresh_local_files)
        self.btn_open_lib = QPushButton("打开目录")
        self.btn_open_lib.setToolTip("在资源管理器中打开所选本地知识库")
        self.btn_open_lib.clicked.connect(self.open_current_local_lib)
        top.addWidget(self.btn_new_lib)
        top.addWidget(self.btn_del_lib)
        top.addWidget(self.btn_open_lib)
        top.addWidget(self.btn_refresh_lib)
        top.addStretch(1)
        lay.addLayout(top)

        hint_lib = QLabel("提示：点「选择本地文件夹」把你已有的文件夹加进来（客户端只读取，"
                          "不会新建或改动里面的文件）。文件按原目录层级显示："
                          "双击文件夹展开/收起，双击文件推送到 ima 知识库；Shift/框选可多选文件。")
        hint_lib.setWordWrap(True)
        lay.addWidget(hint_lib)

        sp = QSplitter(Qt.Horizontal)
        self.lib_list = QListWidget()
        self.lib_list.setMinimumWidth(200)
        self.lib_list.currentRowChanged.connect(lambda _=0: self.refresh_local_files())
        # 文件区：按真实目录层级显示的树（文件夹可展开，文件为叶子）
        self._local_index = {}        # 规范化路径 -> 树节点
        self._local_expanded = []     # 已展开的目录（刷新后恢复）
        self.local_tree = QTreeWidget()
        self.local_tree.setHeaderHidden(True)
        self.local_tree.setUniformRowHeights(True)
        self.local_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.local_tree.setToolTip("双击文件夹展开/收起；双击文件推送到 ima 知识库")
        self.local_tree.itemDoubleClicked.connect(self._local_on_double_click)
        self.local_tree.itemExpanded.connect(self._local_on_expanded)
        self.local_tree.itemCollapsed.connect(self._local_on_collapsed)
        sp.addWidget(self.lib_list)
        sp.addWidget(self.local_tree)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 3)
        lay.addWidget(sp, 1)

        bot = QHBoxLayout()
        bot.addWidget(QLabel("移到 ima 知识库:"))
        self.local_kb_combo = QComboBox()
        self.local_kb_combo.setMinimumWidth(240)
        bot.addWidget(self.local_kb_combo, 1)
        self.btn_load_kbs = QPushButton("加载知识库")
        self.btn_load_kbs.clicked.connect(self.load_local_kb_combo)
        bot.addWidget(self.btn_load_kbs)
        bot.addWidget(QLabel("文件夹ID(可选):"))
        self.local_folder_id = QLineEdit()
        self.local_folder_id.setMaximumWidth(160)
        bot.addWidget(self.local_folder_id)
        self.btn_move_ima = QPushButton("移动到 ima 知识库")
        self.btn_move_ima.clicked.connect(self.move_local_to_ima)
        bot.addWidget(self.btn_move_ima)
        lay.addLayout(bot)

        self.local_log = QPlainTextEdit()
        self.local_log.setReadOnly(True)
        self.local_log.setMaximumHeight(140)
        lay.addWidget(self.local_log)

        self._lib_watcher = QFileSystemWatcher()
        self._lib_watcher.directoryChanged.connect(lambda p: self.refresh_local_files())
        self.refresh_local_libs()
        return w

    def _local_sync_watchers(self):
        """让监视器覆盖「当前库根 + 所有已展开的子目录」，实现自动刷新。"""
        for p in list(self._lib_watcher.directories()):
            self._lib_watcher.removePath(p)
        it = self.lib_list.currentItem()
        if it:
            root = it.data(Qt.UserRole)
            if root and not is_virtual_path(root) and os.path.isdir(root):
                self._lib_watcher.addPath(root)
        for p in self._local_expanded:
            if os.path.isdir(p):
                self._lib_watcher.addPath(p)

    def refresh_local_libs(self):
        self.lib_list.clear()
        for d in self.cfg.local_libs():
            name = os.path.basename(d.rstrip("/\\")) or d
            if is_virtual_path(d) or not os.path.isdir(d):
                text = "%s\n%s   （目录不存在，请移除或重新选择）" % (name, d)
            else:
                text = "%s\n%s" % (name, d)
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, d)
            self.lib_list.addItem(it)
        if self.lib_list.count():
            if self.lib_list.currentRow() < 0:
                self.lib_list.setCurrentRow(0)      # 触发一次 refresh_local_files
            else:
                self.refresh_local_files()
        else:
            self.local_tree.clear()
            self._local_index = {}
            self._local_expanded = []
        self._local_sync_watchers()

    def add_local_lib(self):
        """把本机上一个已有文件夹添加为本地知识库（只读取，不新建/不改动文件）。"""
        dlg = LocalLibDialog(self, self.cfg.local_libs())
        if dlg.exec() != QDialog.Accepted or not dlg.chosen:
            return
        d = dlg.chosen
        self.cfg.add_local_lib(d)
        self.refresh_local_libs()
        for i in range(self.lib_list.count()):
            if self.lib_list.item(i).data(Qt.UserRole) == d:
                self.lib_list.setCurrentRow(i)
                break
        self.local_log.appendPlainText("已添加本地知识库（只读）：%s" % d)

    def _open_local_dir(self, d):
        """在资源管理器中打开目录（仅接受真实目录）。"""
        if is_virtual_path(d) or not os.path.isdir(d):
            QMessageBox.warning(self, "无法打开",
                                "该目录不存在或不是有效文件夹：\n%s" % (d or ""))
            return
        try:
            os.startfile(d)          # Windows 资源管理器
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e)[:200])

    def open_current_local_lib(self):
        it = self.lib_list.currentItem()
        if not it:
            QMessageBox.information(self, "提示", "请先在左侧选择一个本地知识库")
            return
        self._open_local_dir(it.data(Qt.UserRole))

    def del_local_lib(self):
        it = self.lib_list.currentItem()
        if not it:
            return
        d = it.data(Qt.UserRole)
        if QMessageBox.question(self, "移除", "从列表移除本地知识库\n%s？\n（不会删除磁盘文件）" % d,
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.cfg.remove_local_lib(d)
            self.refresh_local_libs()

    # ---- 本地文件树 ----
    def _local_supported(self, path):
        """该扩展名是否在 ima 支持上传的格式表里。"""
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        return ext in ima_openapi.EXT_MAP

    def _local_add_children(self, dirpath, parent_item):
        """把 dirpath 的直接子项挂到树上，子文件夹留占位符以便懒加载。

        返回 (文件夹数, 文件数, 不支持格式数)。
        """
        try:
            names = sorted(os.listdir(dirpath), key=lambda s: s.lower())
        except Exception as e:
            self.local_log.appendPlainText("⚠ 无法读取 %s：%s" % (dirpath, str(e)[:90]))
            return (0, 0, 0)
        dirs, files = [], []
        for n in names:
            (dirs if os.path.isdir(os.path.join(dirpath, n)) else files).append(n)

        def mk(name):
            if parent_item is None:
                return QTreeWidgetItem(self.local_tree)
            return QTreeWidgetItem(parent_item)

        nd = nf = nx = 0
        for n in dirs:
            full = os.path.join(dirpath, n)
            it = mk(n)
            it.setText(0, "📁 " + n)
            it.setData(0, Qt.UserRole, ("dir", full))
            it.setToolTip(0, full)
            QTreeWidgetItem(it, ["载入中…"])       # 占位，撑出展开箭头
            self._local_index[os.path.normcase(full)] = it
            nd += 1
        for n in files:
            full = os.path.join(dirpath, n)
            it = mk(n)
            ok = self._local_supported(full)
            it.setText(0, ("📄 " if ok else "📄⛔ ") + n)
            it.setData(0, Qt.UserRole, ("file", full))
            it.setToolTip(0, full if ok else
                          full + "\n（该格式不在 ima 支持列表内，不会被上传）")
            if not ok:
                it.setForeground(0, QBrush(QColor("#9aa0a6")))
                nx += 1
            self._local_index[os.path.normcase(full)] = it
            nf += 1
        return (nd, nf, nx)

    def _local_on_expanded(self, item):
        data = item.data(0, Qt.UserRole)
        if not data or data[0] != "dir":
            return
        path = data[1]
        if path not in self._local_expanded:
            self._local_expanded.append(path)
        # 首次展开：把占位子项换成真实内容
        if item.childCount() == 1 and item.child(0).data(0, Qt.UserRole) is None:
            item.takeChildren()
            nd, nf, _nx = self._local_add_children(path, item)
            if nf or nd:
                self.local_log.appendPlainText(
                    "  ↳ %s：%d 文件夹 / %d 文件"
                    % (os.path.basename(path) or path, nd, nf))
        self._local_sync_watchers()

    def _local_on_collapsed(self, item):
        data = item.data(0, Qt.UserRole)
        if data and data[0] == "dir" and data[1] in self._local_expanded:
            self._local_expanded.remove(data[1])
        self._local_sync_watchers()

    def _local_on_double_click(self, item, _col=0):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "dir":
            item.setExpanded(not item.isExpanded())     # 双击文件夹：展开/收起
        else:
            self.move_local_to_ima()                    # 双击文件：推送到 ima

    def refresh_local_files(self):
        """重建文件树（顶层立即加载，子层展开时再加载）。"""
        self.local_tree.clear()
        self._local_index = {}
        it = self.lib_list.currentItem()
        if not it:
            return
        root = it.data(Qt.UserRole)
        if is_virtual_path(root) or not os.path.isdir(root):
            self.local_log.appendPlainText("⚠ 目录不存在，已跳过：%s" % (root or ""))
            return
        nd, nf, nx = self._local_add_children(root, None)
        # 恢复上次展开的层级（按父→子顺序展开即可逐层还原）
        keep = [p for p in self._local_expanded if os.path.isdir(p)]
        self._local_expanded = []
        for p in keep:
            node = self._local_index.get(os.path.normcase(p))
            if node is not None:
                node.setExpanded(True)
        self.local_log.appendPlainText(
            "本地库 %s：顶层 %d 个文件夹 / %d 个文件%s（展开文件夹可看下层）"
            % (os.path.basename(root) or root, nd, nf,
               "，其中 %d 个格式不支持已置灰" % nx if nx else " "))
        self._local_sync_watchers()

    def load_local_kb_combo(self):
        cli = self._openapi_client()
        if not cli:
            return
        try:
            kbs = cli.list_knowledge_bases()
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e)[:200])
            return
        self.local_kb_combo.clear()
        for kid, name in (kbs or []):
            self.local_kb_combo.addItem(name, kid)
        self.local_log.appendPlainText("已加载 %d 个 ima 知识库" % self.local_kb_combo.count())

    def move_local_to_ima(self):
        sel = self.local_tree.selectedItems()
        files, dirs = [], []
        for it in sel:
            d = it.data(0, Qt.UserRole)
            if not d:
                continue
            (files if d[0] == "file" else dirs).append(d[1])
        if not files:
            if dirs:
                QMessageBox.information(
                    self, "提示",
                    "选中的是文件夹，文件夹不能直接上传。\n\n"
                    "请双击文件夹展开后，选中里面的文件再上传（可 Shift 连续多选）。")
            else:
                QMessageBox.information(self, "提示",
                                        "请先在右侧选中要上传的本地文件（可多选）")
            return
        unsupported = [p for p in files if not self._local_supported(p)]
        files = [p for p in files if self._local_supported(p)]
        if not files:
            QMessageBox.warning(
                self, "格式不支持",
                "选中的文件都不在 ima 支持上传的格式内：\n%s"
                % "、".join(os.path.basename(p) for p in unsupported[:8]))
            return
        kb_id = self.local_kb_combo.currentData()
        if not kb_id:
            QMessageBox.information(self, "提示", "请先点「加载知识库」并选择目标 ima 知识库")
            return
        folder_id = self.local_folder_id.text().strip() or None
        cli = self._openapi_client()
        if not cli:
            return
        if unsupported:
            self.local_log.appendPlainText(
                "跳过 %d 个格式不支持的文件：%s"
                % (len(unsupported), "、".join(os.path.basename(p) for p in unsupported[:8])))
        paths = files

        def job(w):
            ok = 0
            for p in paths:
                w.log.emit("上传: " + os.path.basename(p))
                res, msg = cli.upload_file(kb_id, folder_id, p,
                                           on_log=lambda m: w.log.emit("  " + m))
                if res:
                    ok += 1
                    w.log.emit("  ✅ " + msg)
                else:
                    w.log.emit("  ❌ " + msg)
            w.log.emit("完成：%d/%d 已进入 ima 知识库" % (ok, len(paths)))

        self._run(job, self.local_log.appendPlainText, None)

    # ---------- 设置 Tab ----------
    def _build_settings_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("一、官方 OpenAPI 凭据（知识库管理/下载）"))
        self.in_cid = QLineEdit(self.cfg.get("openapi_clientid"))
        self.in_cid.setPlaceholderText("ima-openapi-clientid")
        self.in_ckey = QLineEdit(self.cfg.get("openapi_apikey"))
        self.in_ckey.setPlaceholderText("ima-openapi-apikey")
        lay.addWidget(QLabel("clientid:")); lay.addWidget(self.in_cid)
        lay.addWidget(QLabel("apikey:")); lay.addWidget(self.in_ckey)

        lay.addWidget(QLabel("二、非官方 Cookie 通道（ima 原生 LLM，个人使用）"))
        self.in_ck = QLineEdit(self.cfg.get("cookie_xima_cookie"))
        self.in_ck.setPlaceholderText("x-ima-cookie 整串")
        self.in_bkn = QLineEdit(self.cfg.get("cookie_xima_bkn"))
        self.in_bkn.setPlaceholderText("x-ima-bkn 数字")
        lay.addWidget(QLabel("x-ima-cookie:")); lay.addWidget(self.in_ck)
        lay.addWidget(QLabel("x-ima-bkn:")); lay.addWidget(self.in_bkn)

        lay.addWidget(QLabel("三、默认知识库内部 ID（可选）"))
        self.in_kb = QLineEdit(self.cfg.get("default_kb_id"))
        self.in_kb.setPlaceholderText("如 001a9d519e801836")
        lay.addWidget(self.in_kb)

        self.btn_save_cfg = QPushButton("保存设置")
        self.btn_save_cfg.clicked.connect(self.save_settings)
        lay.addWidget(self.btn_save_cfg)
        lay.addStretch(1)
        return w

    def save_settings(self):
        self.cfg.set("openapi_clientid", self.in_cid.text().strip())
        self.cfg.set("openapi_apikey", self.in_ckey.text().strip())
        self.cfg.set("cookie_xima_cookie", self.in_ck.text().strip())
        self.cfg.set("cookie_xima_bkn", self.in_bkn.text().strip())
        self.cfg.set("default_kb_id", self.in_kb.text().strip())
        # 同步对话框默认 kb
        self.chat_kb.setText(self.in_kb.text().strip())
        QMessageBox.information(self, "已保存", "设置已加密保存到本地 config.json")

    # ===== 通用 runner =====
    def _run(self, fn, log_cb=None, bar=None, chunk_cb=None, result_cb=None):
        wk = Worker(fn)
        self._worker = wk
        if not hasattr(self, "_workers"):
            self._workers = []
        self._workers.append(wk)          # 持有引用，防止线程未结束就被 GC

        def _cleanup():
            try:
                self._workers.remove(wk)
            except ValueError:
                pass

        wk.finished.connect(_cleanup)
        if log_cb:
            wk.log.connect(log_cb)
        if bar is not None:
            wk.progress.connect(lambda d, t: (bar.setMaximum(t or 1), bar.setValue(d)))
        if chunk_cb:
            wk.chunk.connect(chunk_cb)
        if result_cb:
            wk.result.connect(result_cb)
        wk.error.connect(lambda e: QMessageBox.critical(self, "错误", e))
        wk.start()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
