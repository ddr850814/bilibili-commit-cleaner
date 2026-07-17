# -*- coding: utf-8 -*-
"""
B站评论清理工具 - GUI 版
基于 tkinter，无需额外安装 GUI 库。
"""

import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# 确保能导入同目录的核心模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bilibili_reply_cleaner as core


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("B站评论批量清理工具")
        self.root.geometry("780x620")
        self.root.minsize(680, 520)

        # 状态
        self.worker_thread = None
        self.confirm_event = threading.Event()
        self.confirm_result = False
        self.is_running = False

        self._build_ui()

    def _build_ui(self):
        # ---- 顶部：标题 + 开始按钮 ----
        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(fill=tk.X)

        ttk.Label(top, text="B站历史回复批量清理", font=("Microsoft YaHei UI", 16, "bold")).pack(side=tk.LEFT)

        self.btn_start = ttk.Button(top, text="开始", command=self.on_start)
        self.btn_start.pack(side=tk.RIGHT)

        # ---- 进度条 + 统计 ----
        prog_frame = ttk.Frame(self.root, padding=(12, 0))
        prog_frame.pack(fill=tk.X)

        self.progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(4, 2))

        self.lbl_stats = ttk.Label(prog_frame, text="就绪", font=("Microsoft YaHei UI", 9))
        self.lbl_stats.pack(anchor=tk.W)

        # ---- 确认按钮（初始隐藏）----
        self.confirm_frame = ttk.Frame(self.root, padding=(12, 4))
        self.lbl_confirm = ttk.Label(self.confirm_frame, text="", font=("Microsoft YaHei UI", 11, "bold"), foreground="red")

        self.btn_confirm_yes = ttk.Button(self.confirm_frame, text="确认删除", command=lambda: self._do_confirm(True))
        self.btn_confirm_no = ttk.Button(self.confirm_frame, text="取消", command=lambda: self._do_confirm(False))

        # ---- 日志区 ----
        log_frame = ttk.Frame(self.root, padding=(12, 8))
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, font=("Consolas", 9), state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # ---- 二维码弹窗引用 ----
        self.qr_window = None
        self.qr_label = None

    # ---------- 日志 ----------
    def log(self, msg=""):
        """线程安全地写入日志"""
        self.root.after(0, self._log_impl, str(msg))

    def _log_impl(self, msg):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ---------- 进度 ----------
    def on_progress(self, idx, total, stats):
        def update():
            self.progress["maximum"] = total
            self.progress["value"] = idx
            self.lbl_stats.config(
                text=f"进度 {idx}/{total}  |  "
                     f"✓已删 {stats['confirmed']}  "
                     f"~已删过 {stats['already_deleted']}  "
                     f"?未验证 {stats['unverified']}  "
                     f"○视频没了 {stats['oid_invalid']}  "
                     f"✗失败 {stats['failed']}"
            )
        self.root.after(0, update)

    # ---------- 二维码 ----------
    def on_qr_callback(self, pil_img):
        """在独立窗口显示二维码"""
        from PIL import ImageTk
        def show():
            if self.qr_window and self.qr_window.winfo_exists():
                self.qr_window.destroy()
            self.qr_window = tk.Toplevel(self.root)
            self.qr_window.title("扫码登录")
            self.qr_window.resizable(False, False)
            # 窗口居中
            self.qr_window.geometry("280x340")
            self.qr_window.update_idletasks()
            cx = self.root.winfo_x() + self.root.winfo_width() // 2 - 140
            cy = self.root.winfo_y() + self.root.winfo_height() // 2 - 170
            self.qr_window.geometry(f"+{cx}+{cy}")

            ttk.Label(self.qr_window, text="请用手机B站App扫码", font=("Microsoft YaHei UI", 11)).pack(pady=(10, 5))

            self.qr_photo = ImageTk.PhotoImage(pil_img.resize((240, 240)))
            self.qr_label = ttk.Label(self.qr_window, image=self.qr_photo)
            self.qr_label.pack(pady=5)

            # 关闭窗口时通知主线程
            self.qr_window.protocol("WM_DELETE_WINDOW", self._on_qr_close)
        self.root.after(0, show)

    def _on_qr_close(self):
        if self.qr_window:
            self.qr_window.destroy()
            self.qr_window = None

    def close_qr_window(self):
        def close():
            if self.qr_window and self.qr_window.winfo_exists():
                self.qr_window.destroy()
                self.qr_window = None
        self.root.after(0, close)

    # ---------- 确认 ----------
    def on_confirm(self, count):
        """弹出确认按钮，阻塞工作线程直到用户点击"""
        def show():
            self.lbl_confirm.config(text=f"已拉取 {count} 条评论，确认全部删除？")
            self.lbl_confirm.pack()
            self.btn_confirm_yes.pack(side=tk.LEFT, padx=(0, 8))
            self.btn_confirm_no.pack(side=tk.LEFT)
            self.confirm_frame.pack(fill=tk.X)
        self.root.after(0, show)

        # 等待用户点击
        self.confirm_event.wait()
        self.confirm_event.clear()

        def hide():
            self.lbl_confirm.pack_forget()
            self.btn_confirm_yes.pack_forget()
            self.btn_confirm_no.pack_forget()
            self.confirm_frame.pack_forget()
        self.root.after(0, hide)

        return self.confirm_result

    def _do_confirm(self, result):
        self.confirm_result = result
        self.confirm_event.set()

    # ---------- 开始 ----------
    def on_start(self):
        if self.is_running:
            return
        self.is_running = True
        self.btn_start.config(state=tk.DISABLED, text="运行中...")
        self.progress["value"] = 0
        self.lbl_stats.config(text="正在启动...")

        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def _worker(self):
        try:
            core.run_pipeline(
                log=self.log,
                confirm_func=self.on_confirm,
                progress_func=self.on_progress,
                qr_callback=self.on_qr_callback,
            )
        except Exception as e:
            self.log(f"\n[错误] {e}")
        finally:
            self.close_qr_window()
            self.root.after(0, self._on_done)

    def _on_done(self):
        self.is_running = False
        self.btn_start.config(state=tk.NORMAL, text="开始")
        self.log("\n— 运行结束 —")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
