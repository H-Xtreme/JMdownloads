#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
JM 本子下载器 · 图形界面版

基于开源项目 hect0x7/JMComic-Crawler-Python 封装的本地下载工具。

功能:
    · 输入车号 / 链接，一键下载整本（支持一次输入多个车号）
    · 下载后可导出为 PDF / ZIP / 长图（按章节拼接）
    · PDF 可加密，密码留空则默认使用本子号
    · 实时日志、进度条、可随时取消

运行方式:
    双击 启动.bat 即可（依赖已随程序放在 libs 目录，整个文件夹可以随意搬走）
"""

import io
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------- 路径与依赖

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 依赖库随程序一起放在 libs 目录里（不用虚拟环境，venv 挪位置就废了，libs 可以随便搬）
LIBS_DIR = os.path.join(APP_DIR, "libs")
if os.path.isdir(LIBS_DIR) and LIBS_DIR not in sys.path:
    sys.path.insert(0, LIBS_DIR)

CONFIG_FILE = os.path.join(APP_DIR, "config.json")
DEFAULT_SAVE_DIR = os.path.join(APP_DIR, "downloads")
LOG_FILE = os.path.join(APP_DIR, "启动日志.txt")

# pythonw 下没有控制台，jmcomic 初始化日志时会往 sys.stdout 写东西，先兜住避免崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def write_log(text: str, mode: str = "a") -> None:
    """把信息写进启动日志，出问题时用户能直接把日志发过来"""
    try:
        with open(LOG_FILE, mode, encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


try:
    import jmcomic
    from jmcomic import DownloadControl, Feature, JmOption

    JM_OK = True
    JM_IMPORT_ERROR = None
except Exception as _e:  # 依赖没装好时给个友好提示，而不是直接闪退
    JM_OK = False
    JM_IMPORT_ERROR = _e

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------- 视觉常量

BG = "#131318"
CARD = "#1c1c23"
CARD2 = "#22222b"
CARD_SOFT = "#23232c"
FIELD = "#26262f"
LOG_BG = "#101015"
TEXT = "#ececf2"
MUTED = "#8b8b99"
ACCENT = "#f0574a"
ACCENT_HOVER = "#d94436"
OK = "#4fbf87"
WARN = "#e6b35c"
ERR = "#ff6b5e"
BORDER = "#2e2e39"

FONT = "Microsoft YaHei UI"
F_TITLE = (FONT, 15, "bold")
F_BODY = (FONT, 10)
F_SMALL = (FONT, 9)
F_TINY = (FONT, 8)
F_MONO = ("Consolas", 9)

SUFFIX_CHOICES = {"原样（推荐）": None, "转 JPG": ".jpg", "转 PNG": ".png", "转 WEBP": ".webp"}
IMPL_CHOICES = {"api（移动端接口 · 兼容性好）": "api", "html（网页端 · 速度快）": "html"}

DEFAULT_CONFIG = {
    "save_dir": DEFAULT_SAVE_DIR,
    "fmt_pdf": True,
    "fmt_zip": False,
    "fmt_long": False,
    "keep_images": True,
    "pdf_encrypt": False,
    "pdf_password": "",
    "client_impl": "api",
    "proxy": "",
    "image_suffix": "原样（推荐）",
    "threads_image": 20,
    "threads_photo": 8,
    "retry_times": 5,
    "domain": "",
}


# ---------------------------------------------------------------- 小工具


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def safe_name(s, maxlen: int = 70) -> str:
    """把本子标题处理成合法的 Windows 文件夹名"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s or "")).strip(" .")
    return (s[:maxlen] or "album").strip()


def parse_ids(text: str) -> list:
    """
    从任意文本里提取车号。
    支持: 123456 / JM123456 / https://18comic.vip/album/123456/xxx
    多个车号可用空格、逗号、换行分隔。
    """
    ids, seen = [], set()
    for token in re.split(r"[\s,，;；、|]+", (text or "").strip()):
        if not token:
            continue
        m = re.search(r"album/(\d+)", token, re.I) or re.search(r"(\d{3,9})", token)
        if not m:
            continue
        aid = m.group(1)
        if aid not in seen:
            seen.add(aid)
            ids.append(aid)
    return ids


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- 下载核心


def build_option(cfg: dict, base_dir: str, after_photo_plugins: list = None) -> "JmOption":
    """按界面配置生成 jmcomic 的 option 对象"""
    suffix = cfg.get("image_suffix")
    suffix = SUFFIX_CHOICES.get(suffix, suffix)
    if suffix == "None":
        suffix = None

    d = {
        "dir_rule": {"base_dir": os.path.abspath(base_dir), "rule": "Bd_Pindex"},
        "download": {
            "cache": True,
            "image": {"decode": True, "suffix": suffix or None},
            "threading": {
                "image": max(1, int(cfg.get("threads_image") or 20)),
                "photo": max(1, int(cfg.get("threads_photo") or 8)),
            },
        },
        "client": {
            "impl": cfg.get("client_impl") or "api",
            "retry_times": max(1, int(cfg.get("retry_times") or 5)),
            # 代理留空 => 显式置空，避免继承系统里那套可能失效的环境变量代理
            "postman": {"type": "curl_cffi", "meta_data": {"proxies": cfg.get("proxy") or None}},
        },
        "log": True,
    }

    domain = (cfg.get("domain") or "").strip()
    if domain:
        d["client"]["domain"] = [x.strip() for x in domain.split(",") if x.strip()]

    if after_photo_plugins:
        d["plugins"] = {"after_photo": after_photo_plugins}

    return JmOption.construct(d)


def resolve_pdf_password(cfg: dict, album_id) -> str:
    """PDF 加密密码：用户填了就用填的，留空则默认用车号（纯数字 ID）"""
    if not cfg.get("pdf_encrypt"):
        return ""
    pwd = str(cfg.get("pdf_password") or "").strip()
    return pwd or str(album_id)


def build_extra(cfg: dict, album_dir: str, album_id, can_delete: bool) -> list:
    """
    生成下载完成后要执行的导出动作。
    PDF / ZIP 走 Feature（整本合并），长图走 after_photo 插件（按章节拼接，
    避免整本拼成一张超大图导致内存爆炸）。
    """
    delete = can_delete and not cfg.get("keep_images", True)
    feats = []
    if cfg.get("fmt_pdf"):
        kw = {
            "pdf_dir": album_dir,
            "delete_original_file": delete and not (cfg.get("fmt_zip") or cfg.get("fmt_long")),
        }
        pwd = resolve_pdf_password(cfg, album_id)
        if pwd:
            kw["encrypt"] = {"password": pwd}
        feats.append(Feature.export_pdf(**kw))
    if cfg.get("fmt_zip"):
        feats.append(Feature.export_zip(zip_dir=album_dir, delete_original_file=delete and not cfg.get("fmt_long")))
    return feats


def count_formats(cfg: dict) -> int:
    return sum(1 for k in ("fmt_pdf", "fmt_zip", "fmt_long") if cfg.get(k))


def long_img_plugins(cfg: dict, album_dir: str) -> list:
    """长图按章节拼接（挂在 after_photo 上），避免整本拼成一张超大图"""
    if not cfg.get("fmt_long"):
        return []
    delete = bool(
        not cfg.get("keep_images", True) and not cfg.get("fmt_pdf") and not cfg.get("fmt_zip")
    )
    return [
        {
            "plugin": "long_img",
            "kwargs": {
                "filename_rule": "[JM{Pid}]{Ptitle}",
                "img_dir": album_dir,
                "delete_original_file": delete,
            },
        }
    ]


class QueueLogHandler(logging.Handler):
    """把 jmcomic 的日志转发到 GUI 队列"""

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        topic = getattr(record, "topic", "") or ""
        level = "error" if record.levelno >= logging.ERROR else ("warn" if record.levelno >= logging.WARNING else "info")
        self.q.put(("log", level, topic, msg))


class Worker(threading.Thread):
    """后台任务线程：查询 or 下载"""

    def __init__(self, ids, cfg, q, mode="download"):
        super().__init__(daemon=True)
        self.ids = ids
        self.cfg = cfg
        self.q = q
        self.mode = mode
        self.control = DownloadControl()
        self._cancelled = False
        self.album_dirs = []

    # --- 对外 ---
    def cancel(self):
        self._cancelled = True
        try:
            self.control.cancel("用户取消")
        except Exception:
            pass

    # --- 内部 ---
    def emit(self, *msg):
        self.q.put(msg)

    def run(self):
        try:
            if self.mode == "query":
                self._run_query()
            else:
                self._run_download()
        except Exception:
            self.emit("log", "error", "", traceback.format_exc())
        finally:
            self.emit("thread_end", self.mode)

    def _new_client(self):
        probe = build_option(self.cfg, self.cfg["save_dir"])
        return probe, probe.new_jm_client()

    def _run_query(self):
        aid = self.ids[0]
        self.emit("status", f"正在查询 JM{aid} …")
        try:
            _, client = self._new_client()
            detail = client.get_album_detail(aid)
        except Exception as e:
            self.emit("log", "error", "", f"查询失败：{e}")
            self.emit("status", "查询失败")
            return
        self.emit("preview", self._detail_dict(detail))
        self.emit("status", "查询完成")
        try:
            url = jmcomic.JmcomicText.get_album_cover_url(str(detail.album_id))
            from curl_cffi import requests as creq

            r = creq.get(url, impersonate="chrome", timeout=10)
            if r.status_code == 200 and r.content:
                self.emit("cover", r.content)
        except Exception:
            pass

    @staticmethod
    def _detail_dict(detail) -> dict:
        def join(v):
            if isinstance(v, (list, tuple, set)):
                return "、".join(str(x) for x in v)
            return str(v or "")

        return {
            "id": str(getattr(detail, "album_id", "") or ""),
            "name": str(getattr(detail, "name", "") or ""),
            "authors": join(getattr(detail, "authors", "")),
            "tags": join(getattr(detail, "tags", "")),
            "pages": int(getattr(detail, "page_count", 0) or 0),
            "chapters": len(detail),
        }

    def _run_download(self):
        n = len(self.ids)
        ok = fail = 0
        summary = []

        try:
            _, client = self._new_client()
        except Exception as e:
            self.emit("log", "error", "", f"初始化失败：{e}")
            self.emit("done", "初始化失败", 0, 0, [])
            return

        for idx, aid in enumerate(self.ids):
            if self._cancelled:
                self.emit("log", "warn", "", "已取消，停止后续任务")
                break

            self.emit("album", aid, idx, n)
            self.emit("status", f"[{idx + 1}/{n}] 获取 JM{aid} 信息…")
            try:
                detail = client.get_album_detail(aid)
            except Exception as e:
                self.emit("log", "error", "", f"JM{aid} 获取详情失败：{e}")
                fail += 1
                continue

            info = self._detail_dict(detail)
            album_dir = os.path.join(self.cfg["save_dir"], f"[JM{aid}]{safe_name(info['name'])}")
            os.makedirs(album_dir, exist_ok=True)
            self.album_dirs.append(album_dir)

            self.emit("preview", info)
            self.emit("pages", info["pages"] or 0)
            self.emit(
                "log",
                "ok",
                "",
                f"《{info['name']}》 作者: {info['authors'] or '未知'} · {info['chapters']} 章 · {info['pages']} 页",
            )

            fmt_n = count_formats(self.cfg)
            if not self.cfg.get("keep_images", True) and fmt_n > 1:
                self.emit("log", "warn", "", "选择了多种导出格式，已自动保留原图（避免后续格式拿不到图片）")

            option = build_option(self.cfg, album_dir, long_img_plugins(self.cfg, album_dir))
            extra = build_extra(self.cfg, album_dir, aid, can_delete=True) or None

            pwd = resolve_pdf_password(self.cfg, aid)
            if pwd:
                self.emit("log", "ok", "", f"PDF 将使用密码：{pwd}")

            self.emit("status", f"[{idx + 1}/{n}] 正在下载《{info['name']}》…")
            try:
                result = jmcomic.download_album(
                    aid, option, extra=extra, control=self.control, check_exception=False
                )
            except Exception as e:
                if "Cancel" in type(e).__name__:
                    self.emit("log", "warn", "", "下载已取消")
                    break
                self.emit("log", "error", "", f"JM{aid} 下载失败：{e}")
                fail += 1
                continue

            # 统计结果
            failed_img = 0
            try:
                failed_img = len(result.downloader.download_failed_image)
            except Exception:
                pass

            exports = []
            try:
                for f in sorted(os.listdir(album_dir)):
                    if f.lower().endswith((".pdf", ".zip", ".7z", ".png")):
                        exports.append(os.path.join(album_dir, f))
            except Exception:
                pass

            size = dir_size(album_dir)
            ok += 1
            summary.append((info["name"], album_dir, exports, size, failed_img))
            self.emit("album_done", info["name"], album_dir, exports, size, failed_img)
            self.emit(
                "log",
                "ok" if not failed_img else "warn",
                "",
                f"完成《{info['name']}》 → {album_dir}（{human_size(size)}"
                + (f"，{len(exports)} 个导出文件" if exports else "")
                + (f"，{failed_img} 张图失败" if failed_img else "")
                + "）",
            )

        self.emit("done", "全部完成" if not self._cancelled else "已取消", ok, fail, summary)


# ---------------------------------------------------------------- 自绘控件
# tkinter 原生控件是直角+系统灰，做不出设计感，这里用 Canvas 自绘一套圆角控件。

from tkinter import font as tkfont


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（平滑多边形近似，支持填充+描边）"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, splinesteps=24, **kw)


class SlimScrollbar(tk.Canvas):
    """自绘的细滚动条（系统自带的滚动条在 Windows 上改不动颜色）"""

    def __init__(self, parent, command, width=10, thumb_color="#33333f", bg=None, **kw):
        super().__init__(parent, width=width, bg=bg or parent["bg"], highlightthickness=0, bd=0, **kw)
        self._cmd = command
        self._sbw = width
        self._first, self._last = 0.0, 1.0
        self._thumb = self.create_rectangle(2, 0, width - 2, 30, fill=thumb_color, outline="")
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except Exception:
            self._first, self._last = 0.0, 1.0
        self._redraw()

    def _redraw(self):
        h = max(1, self.winfo_height())
        span = self._last - self._first
        if span >= 0.999:
            self.itemconfigure(self._thumb, state="hidden")
            return
        top = self._first * h
        bottom = max(top + 24, self._last * h)
        if bottom > h:
            top, bottom = h - 24, h
        self.coords(self._thumb, 2, top, self._sbw - 2, bottom)
        self.itemconfigure(self._thumb, state="normal")

    def _jump(self, event):
        h = max(1, self.winfo_height())
        span = self._last - self._first
        frac = max(0.0, min(1.0, (event.y / h) - span / 2))
        self._cmd("moveto", frac)


class Card(tk.Canvas):
    """圆角卡片容器；内容放进 .body"""

    def __init__(self, parent, fill=CARD, outline=BORDER, radius=14, padx=16, pady=14, bg=BG, stretch=False):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, height=10)
        self._fill, self._outline, self._radius = fill, outline, radius
        self._padx, self._pady, self._stretch = padx, pady, stretch
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(padx, pady, window=self.body, anchor="nw")
        self.bind("<Configure>", self._draw)

    def _sync_height(self, event=None):
        if self._stretch:
            return
        need = self.body.winfo_reqheight() + 2 * self._pady
        if abs(self.winfo_height() - need) > 1:
            self.configure(height=need)

    def _draw(self, event=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 2 or h <= 2:
            return
        self.delete("bgshape")
        round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._radius,
                   fill=self._fill, outline=self._outline, width=1, tags="bgshape")
        self.tag_lower("bgshape")
        if self._stretch:
            self.itemconfigure(self._win, width=max(1, w - 2 * self._padx),
                               height=max(1, h - 2 * self._pady))
        else:
            self.itemconfigure(self._win, width=max(1, w - 2 * self._padx))
            self.after_idle(self._sync_height)


class RButton(tk.Canvas):
    """圆角按钮"""

    STYLES = {
        "accent": dict(fill=ACCENT, fg="#ffffff", hover=ACCENT_HOVER, outline=""),
        "ghost": dict(fill=CARD_SOFT, fg=TEXT, hover=FIELD, outline=BORDER),
        "subtle": dict(fill="", fg=MUTED, hover=CARD_SOFT, outline=""),
    }

    def __init__(self, parent, text, command, kind="ghost", width=None, height=34,
                 font=F_BODY, radius=9, bg=CARD):
        st = dict(self.STYLES.get(kind, self.STYLES["ghost"]))
        f = tkfont.Font(font=font)
        w = width or (f.measure(text) + 30)
        super().__init__(parent, width=w, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._bg, self._st, self._cw, self._ch, self._r = bg, st, w, height, radius
        self._cmd, self._enabled, self._hover = command, True, False
        self._tid = self.create_text(w / 2, height / 2 + 1, text=text, fill=st["fg"], font=font)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._paint()

    def _paint(self):
        self.delete("bg")
        fill = self._st["fill"] or self._bg
        fg = self._st["fg"]
        if not self._enabled:
            fill = CARD_SOFT if self._st["fill"] else self._bg
            fg = "#55555f"
        elif self._hover:
            fill = self._st["hover"] or fill
        if fill and fill != self._bg:
            round_rect(self, 0.5, 0.5, self._cw - 0.5, self._ch - 0.5, self._r,
                       fill=fill, outline=self._st["outline"] or fill, width=1, tags="bg")
            self.tag_lower("bg")
        self.itemconfigure(self._tid, fill=fg)

    def _on_enter(self, _=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _=None):
        self._hover = False
        self._paint()

    def _on_press(self, _=None):
        if self._enabled:
            self.move(self._tid, 0, 1)

    def _on_release(self, _=None):
        if not self._enabled:
            return
        self.move(self._tid, 0, -1)
        try:
            self._cmd()
        except Exception:
            pass

    # 兼容 .config(state="disabled")
    def config(self, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            self.configure(cursor="hand2" if self._enabled else "arrow")
            self._paint()
        if kw:
            super().config(**kw)

    configure = config


class RInput(tk.Canvas):
    """圆角输入框（内部包一个无边框 Entry）"""

    def __init__(self, parent, textvariable, width=180, height=34, radius=9,
                 bg=CARD, font=F_BODY, show=None, on_return=None):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
        self._bg, self._cw, self._ch, self._r = bg, width, height, radius
        self._focus = False
        self._enabled = True
        self.entry = tk.Entry(self, textvariable=textvariable, font=font, bg=FIELD, fg=TEXT,
                              insertbackground=ACCENT, relief="flat", bd=0, highlightthickness=0,
                              disabledbackground="#1f1f27", disabledforeground="#5f5f6b", show=show)
        self._win = self.create_window(12, height / 2, window=self.entry, anchor="w", width=width - 24)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", self._draw)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        if on_return:
            self.entry.bind("<Return>", lambda e: on_return())
        self._draw()

    def _on_click(self, _=None):
        if self._enabled:
            self.entry.focus_set()

    def _on_focus_in(self, _=None):
        self._focus = True
        self._draw()

    def _on_focus_out(self, _=None):
        self._focus = False
        self._draw()

    def _draw(self, _=None):
        w = self.winfo_width() or self._cw
        h = self.winfo_height() or self._ch
        self.delete("bg")
        outline = ACCENT if self._focus else BORDER
        fill = FIELD
        if not self._enabled:
            outline, fill = "#26262e", "#1f1f27"
        round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._r, fill=fill, outline=outline, width=1, tags="bg")
        self.tag_lower("bg")
        self.coords(self._win, 12, h / 2)
        self.itemconfigure(self._win, width=max(1, w - 24))

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.entry.config(state="normal" if enabled else "disabled",
                          disabledbackground="#1f1f27" if not enabled else FIELD)
        self.configure(cursor="xterm" if enabled else "arrow")
        self._draw()


class RCheck(tk.Canvas):
    """圆角勾选框"""

    def __init__(self, parent, text, variable, bg=CARD, font=F_BODY, box=18):
        f = tkfont.Font(font=font)
        w = box + 9 + f.measure(text) + 2
        super().__init__(parent, width=w, height=26, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._var, self._box, self._bg, self._font = variable, box, bg, font
        self._text, self._hover, self._enabled = text, False, True
        self._tid = self.create_text(box + 9, 14, text=text, anchor="w", fill=TEXT, font=font)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._var.trace_add("write", lambda *a: self._paint())
        self._paint()

    def _toggle(self, _=None):
        if self._enabled:
            self._var.set(not self._var.get())

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def _on_enter(self, _=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _=None):
        self._hover = False
        self._paint()

    def _paint(self):
        self.delete("bx")
        on = bool(self._var.get())
        b = self._box
        if on:
            round_rect(self, 1, 5, 1 + b, 5 + b, 5, fill=ACCENT, outline=ACCENT, width=1, tags="bx")
            self.create_line(5, 5 + b / 2 + 0.5, 8.5, 5 + b - 4.5, 14, 5 + 4.5,
                             fill="#ffffff", width=2, capstyle="round", joinstyle="round", tags="bx")
        else:
            oc = "#4a4a58" if (self._hover and self._enabled) else BORDER
            round_rect(self, 1, 5, 1 + b, 5 + b, 5, fill=CARD_SOFT, outline=oc, width=1, tags="bx")
        self.tag_raise("bx")
        if not self._enabled:
            fg = "#55555f"
        else:
            fg = "#ffffff" if self._hover else TEXT
        self.itemconfigure(self._tid, fill=fg)


class RProgress(tk.Canvas):
    """圆角进度条，支持确定/不确定两种模式"""

    def __init__(self, parent, height=10, bg=BG, track=CARD_SOFT, fill=ACCENT, radius=5):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0)
        self._ch, self._track, self._fill, self._r = height, track, fill, radius
        self._frac = 0.0
        self._indet = False
        self._phase = 0.0
        self._job = None
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, frac):
        self._indet = False
        self._stop_anim()
        self._frac = max(0.0, min(1.0, float(frac or 0)))
        self._redraw()

    def start(self):
        if self._indet:
            return
        self._indet = True
        self._phase = 0.0
        self._anim()

    def stop(self):
        self._indet = False
        self._stop_anim()
        self._redraw()

    def _stop_anim(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _anim(self):
        if not self._indet:
            return
        self._phase = (self._phase + 0.02) % 1.0
        self._redraw()
        self._job = self.after(28, self._anim)

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 4 or h <= 2:
            return
        round_rect(self, 0, 0, w, h, self._r, fill=self._track, outline=self._track)
        if self._indet:
            seg = w * 0.28
            x = -seg + self._phase * (w + seg)
            x1, x2 = max(0, x), min(w, x + seg)
            if x2 > x1 + 2:
                round_rect(self, x1, 0, x2, h, self._r, fill=self._fill, outline=self._fill)
        elif self._frac > 0:
            fw = max(h, w * self._frac)
            round_rect(self, 0, 0, fw, h, self._r, fill=self._fill, outline=self._fill)


class Pill(tk.Canvas):
    """小圆角标签"""

    def __init__(self, parent, text, bg=CARD, fill=CARD_SOFT, fg=MUTED, font=F_TINY, padx=9, height=22):
        f = tkfont.Font(font=font)
        w = f.measure(text) + padx * 2
        super().__init__(parent, width=w, height=height, bg=bg, highlightthickness=0, bd=0)
        round_rect(self, 0.5, 0.5, w - 0.5, height - 0.5, height / 2 - 1, fill=fill, outline=fill)
        self._tid = self.create_text(w / 2, height / 2, text=text, fill=fg, font=font)
        self._bg, self._fill, self._fg = bg, fill, fg

    def set(self, text):
        f = tkfont.Font(font=F_TINY)
        w = f.measure(text) + 18
        self.delete("all")
        self.configure(width=w)
        round_rect(self, 0.5, 0.5, w - 0.5, 21.5, 10, fill=self._fill, outline=self._fill)
        self.create_text(w / 2, 11, text=text, fill=self._fg, font=F_TINY)


# ---------------------------------------------------------------- 主界面


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()
        self.worker = None
        self.total_pages = 0
        self.done_pages = 0
        self.current_album_dir = None
        self._cover_img = None
        self.placeholder = "输入车号，例如 123456（支持网页链接，多个用空格分隔）"

        root.title("JM 本子下载器")
        root.configure(bg=BG)
        root.geometry("980x820")
        root.minsize(900, 700)

        self._init_style()
        self._build_ui()
        self._install_log_handler()
        self._sync_pwd_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain)

    # ---------- 样式 ----------
    def _init_style(self):
        try:  # 高分屏清晰一点
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(
            "Dark.TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=FIELD,
            darkcolor=FIELD,
            selectbackground=FIELD,
            selectforeground=TEXT,
            padding=4,
        )
        st.map("Dark.TCombobox",
               fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", TEXT)],
               bordercolor=[("focus", ACCENT), ("!focus", BORDER)])
        self.root.option_add("*TCombobox*Listbox.background", CARD_SOFT)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", F_BODY)

    # ---------- 小工具 ----------
    def _label(self, parent, text, font=F_BODY, fg=TEXT, bg=CARD, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=bg, **kw)

    def _col(self, parent, text, width=64):
        """固定宽度的左侧小标题，保证各行对齐"""
        f = tk.Frame(parent, bg=parent["bg"], width=width, height=26)
        f.pack(side="left")
        f.pack_propagate(False)
        tk.Label(f, text=text, font=F_SMALL, fg=MUTED, bg=parent["bg"], anchor="w").place(x=0, rely=0.5, anchor="w")
        return f

    def _combo(self, parent, var, values, width=18):
        return ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                            width=width, style="Dark.TCombobox", font=F_SMALL)

    def _divider(self, parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=11)

    # ---------- 布局 ----------
    def _build_ui(self):
        root = self.root
        outer = tk.Frame(root, bg=BG)
        outer.pack(fill="both", expand=True, padx=18, pady=14)

        # ---- 顶栏 ----
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        logo = tk.Canvas(head, width=38, height=38, bg=BG, highlightthickness=0)
        logo.pack(side="left")
        round_rect(logo, 1, 1, 37, 37, 12, fill=ACCENT, outline=ACCENT)
        logo.create_text(19, 20, text="JM", fill="#ffffff", font=(FONT, 10, "bold"))

        tbox = tk.Frame(head, bg=BG)
        tbox.pack(side="left", padx=(11, 0))
        self._label(tbox, "本子下载器", font=(FONT, 15, "bold"), bg=BG).pack(anchor="w")
        self._label(tbox, "输入车号 · 一键下载 · 导出 PDF / ZIP / 长图", font=F_TINY, fg=MUTED, bg=BG).pack(anchor="w")

        ver = getattr(jmcomic, "__version__", "") if JM_OK else ""
        self._label(head, f"JMComic-Crawler-Python {ver}".strip(), font=F_TINY, fg="#4d4d5a", bg=BG).pack(
            side="right", pady=(18, 0)
        )

        # ---- 输入卡 ----
        cin = Card(outer)
        cin.pack(fill="x", pady=(14, 0))
        b = cin.body
        row = tk.Frame(b, bg=CARD)
        row.pack(fill="x")
        self.var_input = tk.StringVar()
        self.inp = RInput(row, self.var_input, height=38, bg=CARD, on_return=self.start_download)
        self.inp.pack(side="left", fill="x", expand=True)
        self.entry_input = self.inp.entry
        self.entry_input.insert(0, self.placeholder)
        self.entry_input.config(fg=MUTED)
        self.entry_input.bind("<FocusIn>", self._on_focus_in, add="+")
        self.entry_input.bind("<FocusOut>", self._on_focus_out, add="+")

        self.btn_query = RButton(row, "查询", self.start_query, "ghost", height=38, bg=CARD)
        self.btn_query.pack(side="left", padx=(8, 0))
        self.btn_start = RButton(row, "开始下载", self.start_download, "accent", height=38, bg=CARD)
        self.btn_start.pack(side="left", padx=(8, 0))
        self.btn_cancel = RButton(row, "取消", self.cancel, "ghost", height=38, bg=CARD)
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.btn_cancel.config(state="disabled")

        self._label(b, "支持 123456 / JM123456 / 网页链接；多个车号用空格或逗号分隔", font=F_TINY, fg=MUTED).pack(
            anchor="w", pady=(9, 0)
        )

        # ---- 信息卡 ----
        cinfo = Card(outer)
        cinfo.pack(fill="x", pady=(10, 0))
        b = cinfo.body
        self.cover_lbl = tk.Label(b, bg=CARD_SOFT, width=8, height=5, text="封面", fg="#45454f", font=F_TINY)
        self.cover_lbl.pack(side="left")

        info = tk.Frame(b, bg=CARD)
        info.pack(side="left", fill="both", expand=True, padx=(14, 0))
        self.pill_id = Pill(info, "JM —", bg=CARD, fill="#33262a", fg=ACCENT, font=F_TINY)
        self.pill_id.pack(anchor="w")
        self.lbl_title = self._label(info, "等待输入车号…", font=(FONT, 12, "bold"), anchor="w", justify="left")
        self.lbl_title.pack(fill="x", pady=(6, 0))
        self.lbl_meta = self._label(info, "点「查询」可先看详情，也可以直接下载", font=F_SMALL, fg=MUTED, anchor="w")
        self.lbl_meta.pack(fill="x", pady=(3, 0))
        self.lbl_tags = self._label(info, "", font=F_TINY, fg="#6d6d7c", anchor="w", justify="left", wraplength=720)
        self.lbl_tags.pack(fill="x", pady=(4, 0))

        # ---- 设置卡 ----
        copt = Card(outer)
        copt.pack(fill="x", pady=(10, 0))
        b = copt.body

        r1 = tk.Frame(b, bg=CARD)
        r1.pack(fill="x")
        self._col(r1, "保存目录")
        self.var_dir = tk.StringVar(value=self.cfg["save_dir"])
        self.inp_dir = RInput(r1, self.var_dir, height=32, bg=CARD)
        self.inp_dir.pack(side="left", fill="x", expand=True, padx=(0, 8))
        RButton(r1, "浏览", self.pick_dir, "ghost", height=32, bg=CARD).pack(side="left")
        RButton(r1, "打开", lambda: self.open_path(self.var_dir.get()), "ghost", height=32, bg=CARD).pack(
            side="left", padx=(6, 0)
        )

        self._divider(b)

        r2 = tk.Frame(b, bg=CARD)
        r2.pack(fill="x")
        self._col(r2, "导出格式")
        self.var_pdf = tk.BooleanVar(value=self.cfg["fmt_pdf"])
        self.var_zip = tk.BooleanVar(value=self.cfg["fmt_zip"])
        self.var_long = tk.BooleanVar(value=self.cfg["fmt_long"])
        self.var_keep = tk.BooleanVar(value=self.cfg["keep_images"])
        RCheck(r2, "PDF 整本合并", self.var_pdf, bg=CARD).pack(side="left")
        RCheck(r2, "ZIP 整本打包", self.var_zip, bg=CARD).pack(side="left", padx=(16, 0))
        RCheck(r2, "长图 按章节", self.var_long, bg=CARD).pack(side="left", padx=(16, 0))
        RCheck(r2, "保留原图", self.var_keep, bg=CARD).pack(side="left", padx=(16, 0))
        self.var_pdf.trace_add("write", lambda *a: self._sync_pwd_state())

        r3 = tk.Frame(b, bg=CARD)
        r3.pack(fill="x", pady=(9, 0))
        self._col(r3, "PDF 密码")
        self.var_pdf_lock = tk.BooleanVar(value=self.cfg["pdf_encrypt"])
        self.chk_lock = RCheck(r3, "加密", self.var_pdf_lock, bg=CARD)
        self.chk_lock.pack(side="left")
        self.var_pdf_pwd = tk.StringVar(value=self.cfg["pdf_password"])
        self.inp_pwd = RInput(r3, self.var_pdf_pwd, width=190, height=32, bg=CARD)
        self.inp_pwd.pack(side="left", padx=(12, 10))
        self._label(r3, "留空 = 用本子号（JM 后面那串数字）", font=F_TINY, fg="#6d6d7c").pack(side="left")
        self.var_pdf_lock.trace_add("write", lambda *a: self._sync_pwd_state())

        self._divider(b)

        r4 = tk.Frame(b, bg=CARD)
        r4.pack(fill="x")
        self._col(r4, "高级")
        self._label(r4, "接口", font=F_TINY, fg=MUTED).pack(side="left")
        self.var_impl_label = tk.StringVar(
            value=next((k for k, v in IMPL_CHOICES.items() if v == self.cfg["client_impl"]), list(IMPL_CHOICES)[0])
        )
        self._combo(r4, self.var_impl_label, list(IMPL_CHOICES.keys()), width=23).pack(side="left", padx=(6, 16))

        self._label(r4, "代理", font=F_TINY, fg=MUTED).pack(side="left")
        self.var_proxy = tk.StringVar(value=self.cfg["proxy"])
        self.inp_proxy = RInput(r4, self.var_proxy, width=170, height=30, bg=CARD)
        self.inp_proxy.pack(side="left", padx=(6, 16))

        self._label(r4, "图片格式", font=F_TINY, fg=MUTED).pack(side="left")
        self.var_suffix = tk.StringVar(value=self.cfg["image_suffix"])
        self._combo(r4, self.var_suffix, list(SUFFIX_CHOICES.keys()), width=12).pack(side="left", padx=(6, 16))

        self._label(r4, "线程", font=F_TINY, fg=MUTED).pack(side="left")
        self.var_threads = tk.StringVar(value=str(self.cfg["threads_image"]))
        tk.Spinbox(
            r4, from_=1, to=64, textvariable=self.var_threads, width=4, font=F_SMALL,
            bg=FIELD, fg=TEXT, buttonbackground=CARD_SOFT, insertbackground=ACCENT,
            relief="flat", bd=0, highlightthickness=0,
        ).pack(side="left", padx=(6, 0))

        # ---- 进度 ----
        prow = tk.Frame(outer, bg=BG)
        prow.pack(fill="x", pady=(13, 0))
        self.var_status = tk.StringVar(value="就绪")
        self._label(prow, "", textvariable=self.var_status, font=F_SMALL, fg="#a9a9b8", bg=BG).pack(side="left")
        self.var_pct = tk.StringVar(value="")
        self._label(prow, "", textvariable=self.var_pct, font=F_SMALL, fg=ACCENT, bg=BG).pack(side="right")

        self.bar = RProgress(outer, height=10)
        self.bar.pack(fill="x", pady=(7, 10))

        # ---- 日志卡 ----
        clog = Card(outer, pady=12, stretch=True)
        clog.pack(fill="both", expand=True)
        b = clog.body
        lh = tk.Frame(b, bg=CARD)
        lh.pack(fill="x")
        self._label(lh, "运行日志", font=(FONT, 9, "bold"), fg="#a9a9b8", bg=CARD).pack(side="left")
        RButton(lh, "打开下载目录", lambda: self.open_path(self.var_dir.get()), "subtle",
                height=24, font=F_TINY, bg=CARD).pack(side="right")
        RButton(lh, "清空", self.clear_log, "subtle", height=24, font=F_TINY, bg=CARD).pack(side="right", padx=(0, 2))

        logwrap = tk.Frame(b, bg=LOG_BG)
        logwrap.pack(fill="both", expand=True, pady=(9, 0))
        self.log = tk.Text(
            logwrap, bg=LOG_BG, fg="#c9c9d4", font=F_MONO, relief="flat", bd=0,
            wrap="word", padx=10, pady=8, insertbackground=ACCENT, state="disabled",
            spacing1=1, spacing3=1,
        )
        self.log.pack(side="left", fill="both", expand=True)
        sb = SlimScrollbar(logwrap, command=self.log.yview, bg=LOG_BG, width=10)
        sb.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=sb.set)
        self.log.tag_config("info", foreground="#c9c9d4")
        self.log.tag_config("warn", foreground=WARN)
        self.log.tag_config("error", foreground=ERR)
        self.log.tag_config("ok", foreground=OK)
        self.log.tag_config("dim", foreground="#5f5f6b")

        self._append_log("dim", "就绪。输入车号后回车即可开始下载。")
        if not JM_OK:
            self._append_log("error", f"jmcomic 导入失败：{JM_IMPORT_ERROR}")
            self._append_log("error", "请先双击同目录下的「安装依赖.bat」")

    # ---------- 密码框联动 ----------
    def _sync_pwd_state(self):
        pdf_on = bool(self.var_pdf.get())
        lock_on = bool(self.var_pdf_lock.get()) and pdf_on
        self.chk_lock.set_enabled(pdf_on)
        self.inp_pwd.set_enabled(lock_on)

    # ---------- 日志 ----------
    def _install_log_handler(self):
        if not JM_OK:
            return
        self.handler = QueueLogHandler(self.q)
        self.handler.setLevel(logging.INFO)
        lg = logging.getLogger("jmcomic")
        lg.setLevel(logging.INFO)
        lg.addHandler(self.handler)

    def _append_log(self, level, text, ts=True):
        self.log.configure(state="normal")
        if ts:
            self.log.insert("end", time.strftime("%H:%M:%S  "), "dim")
        self.log.insert("end", text + "\n", level)
        try:
            lines = int(self.log.index("end-1c").split(".")[0])
            if lines > 3000:
                self.log.delete("1.0", f"{lines - 2500}.0")
        except Exception:
            pass
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- 事件队列 ----------
    def _drain(self):
        try:
            for _ in range(300):
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(80, self._drain)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "log":
            _, level, topic, text = msg
            if topic in ("image.before",):
                return  # 太啰嗦，只看完成
            self._append_log(level, text)
            if topic == "image.after":
                self.done_pages += 1
                self._update_bar()
        elif kind == "status":
            self.var_status.set(msg[1])
        elif kind == "pages":
            self.total_pages += int(msg[1] or 0)
            self._update_bar()
        elif kind == "album":
            _, aid, idx, total = msg
            self.done_pages = 0
            self.total_pages = 0
            self.bar.set(0)
            self.var_pct.set("")
            self.var_status.set(f"[{idx + 1}/{total}] JM{aid}")
        elif kind == "preview":
            self._show_preview(msg[1])
        elif kind == "cover":
            self._show_cover(msg[1])
        elif kind == "album_done":
            _, name, path, exports, size, failed = msg
            self.current_album_dir = path
            self.bar.set(1.0)
            self.var_pct.set("100%")
        elif kind == "done":
            _, text, ok, fail, summary = msg
            self._set_running(False)
            self.var_status.set(text)
            if ok and not fail:
                self.bar.set(1.0)
                self.var_pct.set("100%")
            self._append_log("ok" if not fail else "warn", f"—— {text}：成功 {ok} 本，失败 {fail} 本 ——")
            if summary and self.current_album_dir and os.path.isdir(self.current_album_dir):
                if messagebox.askyesno("下载完成", f"{text}\n成功 {ok} 本，失败 {fail} 本\n\n要打开下载目录吗？"):
                    self.open_path(self.current_album_dir)
        elif kind == "thread_end":
            self._set_running(False)

    def _update_bar(self):
        if self.total_pages <= 0:
            self.bar.start()
            self.var_pct.set("")
            return
        frac = min(1.0, self.done_pages / self.total_pages)
        self.bar.set(frac)
        self.var_pct.set(f"{frac * 100:.0f}%   {self.done_pages}/{self.total_pages}")

    def _show_preview(self, info):
        self.pill_id.set(f"JM {info['id']}")
        title = info["name"] or "(无标题)"
        if len(title) > 46:
            title = title[:46] + "…"
        self.lbl_title.config(text=title)
        self.lbl_meta.config(
            text=f"作者 {info['authors'] or '未知'}    ·    {info['chapters']} 章    ·    {info['pages']} 页"
        )
        tags = info["tags"] or ""
        if len(tags) > 90:
            tags = tags[:90] + "…"
        self.lbl_tags.config(text=("标签  " + tags) if tags else "")

    def _show_cover(self, data):
        try:
            from PIL import Image, ImageTk

            im = Image.open(io.BytesIO(data))
            im.thumbnail((96, 132))
            self._cover_img = ImageTk.PhotoImage(im)
            self.cover_lbl.config(image=self._cover_img, text="", width=96, height=132)
        except Exception:
            pass

    # ---------- 动作 ----------
    def _set_running(self, running: bool):
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_query.config(state="disabled" if running else "normal")
        self.btn_cancel.config(state="normal" if running else "disabled")

    def _collect_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg.update(
            {
                "save_dir": self.var_dir.get().strip() or DEFAULT_SAVE_DIR,
                "fmt_pdf": bool(self.var_pdf.get()),
                "fmt_zip": bool(self.var_zip.get()),
                "fmt_long": bool(self.var_long.get()),
                "keep_images": bool(self.var_keep.get()),
                "pdf_encrypt": bool(self.var_pdf_lock.get()),
                "pdf_password": self.var_pdf_pwd.get().strip(),
                "client_impl": IMPL_CHOICES.get(self.var_impl_label.get(), "api"),
                "proxy": self.var_proxy.get().strip(),
                "image_suffix": self.var_suffix.get(),
                "threads_image": self._to_int(self.var_threads.get(), 20),
            }
        )
        return cfg

    # ---------- 输入框占位符 ----------
    def _on_focus_in(self, _=None):
        if self.var_input.get() == self.placeholder:
            self.var_input.set("")
            self.entry_input.config(fg=TEXT)

    def _on_focus_out(self, _=None):
        if not self.var_input.get().strip():
            self.var_input.set(self.placeholder)
            self.entry_input.config(fg=MUTED)

    def _input_text(self) -> str:
        t = self.var_input.get().strip()
        return "" if t == self.placeholder else t

    @staticmethod
    def _to_int(v, default):
        try:
            return max(1, int(str(v).strip()))
        except Exception:
            return default

    def start_query(self):
        if self.worker and self.worker.is_alive():
            return
        ids = parse_ids(self._input_text())
        if not ids:
            messagebox.showwarning("提示", "没识别到车号，请输入数字 ID 或本子链接")
            return
        if not JM_OK:
            messagebox.showerror("依赖缺失", f"jmcomic 未安装：{JM_IMPORT_ERROR}")
            return
        cfg = self._collect_cfg()
        cfg["save_dir"] = os.path.abspath(cfg["save_dir"])
        self._set_running(True)
        self.worker = Worker([ids[0]], cfg, self.q, mode="query")
        self.worker.start()

    def start_download(self):
        if self.worker and self.worker.is_alive():
            return
        ids = parse_ids(self._input_text())
        if not ids:
            messagebox.showwarning("提示", "没识别到车号，请输入数字 ID 或本子链接")
            return
        if not JM_OK:
            messagebox.showerror("依赖缺失", f"jmcomic 未安装：{JM_IMPORT_ERROR}")
            return
        cfg = self._collect_cfg()
        if not cfg["keep_images"] and count_formats(cfg) == 0:
            messagebox.showwarning("提示", "不保留原图时必须至少选一种导出格式")
            return
        cfg["save_dir"] = os.path.abspath(cfg["save_dir"])
        try:
            os.makedirs(cfg["save_dir"], exist_ok=True)
        except Exception as e:
            messagebox.showerror("目录错误", f"无法创建保存目录：{e}")
            return

        self.cfg = cfg
        save_config(cfg)

        self.total_pages = 0
        self.done_pages = 0
        self.bar.set(0)
        self.var_pct.set("")
        self._set_running(True)
        self._append_log("dim", f"开始任务：{len(ids)} 本 · 保存到 {cfg['save_dir']}")
        self.worker = Worker(ids, cfg, self.q, mode="download")
        self.worker.start()

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.worker.cancel()
            self.var_status.set("正在取消…")
            self._append_log("warn", "已请求取消，等待当前请求结束…")

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_dir.get() or APP_DIR, title="选择保存目录")
        if d:
            self.var_dir.set(os.path.normpath(d))

    @staticmethod
    def open_path(path):
        path = (path or "").strip()
        if not path or not os.path.exists(path):
            messagebox.showinfo("提示", "目录还不存在，下载完成后会自动创建")
            return
        try:
            os.startfile(path)  # noqa: S606
        except Exception:
            subprocess.Popen(["explorer", os.path.normpath(path)])

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("退出", "任务还在进行中，确定要退出吗？"):
                return
            self.worker.cancel()
            self.worker.join(timeout=2)
        try:
            save_config(self._collect_cfg())
        except Exception:
            pass
        if JM_OK:
            try:
                logging.getLogger("jmcomic").removeHandler(self.handler)
            except Exception:
                pass
        self.root.destroy()


def _env_header() -> str:
    ver = getattr(jmcomic, "__version__", "?") if JM_OK else f"导入失败: {JM_IMPORT_ERROR!r}"
    return (
        f"===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        f"Python   : {sys.version}\n"
        f"可执行文件: {sys.executable}\n"
        f"程序目录  : {APP_DIR}\n"
        f"依赖目录  : {LIBS_DIR}（存在: {os.path.isdir(LIBS_DIR)}）\n"
        f"jmcomic  : {ver}\n\n"
    )


def main():
    write_log(_env_header(), "w")

    try:
        root = tk.Tk()
    except Exception:
        tb = traceback.format_exc()
        write_log("创建窗口失败:\n" + tb)
        print(tb)
        return

    def report_callback_exception(exc, val, tb_obj):
        text = "".join(traceback.format_exception(exc, val, tb_obj))
        write_log("界面回调异常:\n" + text)
        try:
            messagebox.showerror("出错了", f"{val}\n\n详情见：\n{LOG_FILE}")
        except Exception:
            pass

    root.report_callback_exception = report_callback_exception

    try:
        App(root)
        if not JM_OK:
            root.after(
                400,
                lambda: messagebox.showerror(
                    "依赖缺失",
                    f"没能加载下载组件：\n{JM_IMPORT_ERROR}\n\n"
                    f"请先运行同目录下的「安装依赖.bat」。",
                ),
            )
        root.mainloop()
    except Exception:
        tb = traceback.format_exc()
        write_log("启动异常:\n" + tb)
        try:
            messagebox.showerror("启动失败", f"程序启动失败：\n\n{tb[-1200:]}\n\n完整日志：\n{LOG_FILE}")
        except Exception:
            print(tb)


if __name__ == "__main__":
    main()
