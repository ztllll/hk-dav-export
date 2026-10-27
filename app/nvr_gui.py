#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nvr_gui.py — NVR 录像导出工具 GUI 版(tkinter 单文件)

与 CLI 共用协议引擎 scripts/nvr_dump.py(import 复用,协议细节勿改,见 docs/LESSONS.md)。
流程:①连接检测→②勾选通道→③时间窗/目标目录/命名→④[预检并导出]
     预检=枚举段数/大小/估时/查目标盘,弹窗确认→实时进度下载→自动写 说明.txt
断点续传:重跑自动跳过已存在文件(.part 临时文件 + DHII 校验,与 CLI 同机制)。
"""
import base64
import json
import os
import re
import sys
import queue
import shutil
import threading
import time
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "scripts"))
import nvr_dump as nd                       # 大华协议引擎(复用,不重写)
import hik_dump as hkd                      # 海康 ISAPI 适配(未经实测,见模块说明)
from requests.auth import HTTPDigestAuth

DEFAULT_CONNS = "\n".join("%s,admin,your_password" % ip
                          for ip in ("10.10.50.1", "10.10.50.2", "10.10.50.3", "10.10.50.4"))
EST_SPEED = 25e6                            # 预检估速:NTFS 移动硬盘实测 ~25 MB/s(docs/LESSONS.md)
README_NAME = "说明.txt"
README_TEXT = """================ NVR 录像播放说明 ================

一、文件格式
  大华 NVR 导出:.dav 原始录像(未转码)
  海康 NVR 导出:.mp4(PS 流,内容未转码)

二、怎么播放
  1) 大华 .dav:大华官方播放器 SmartPlayer
     - 大华官网支持中心 → 工具软件:https://www.dahuatech.com
     - 或联系管理员获取安装包
     - 打开 SmartPlayer → 文件 → 打开 → 选择 .dav 文件即可播放
  2) 海康 .mp4:VLC media player 或 海康 iVMS-4200 客户端均可直接播放

三、应急播放(可选)
  VLC 可尝试直接打开 .dav;打不开请勿批量改后缀,一律以官方播放器为准。

四、文件命名规则
  <通道名>_<开始时间>_<结束时间>_<源编号>.dav 或 .mp4
  由 NVR 录像导出工具自动生成,请勿改名,以便追溯。

五、导出失败/需要重新导出
  重新打开导出工具,选择相同通道与时间窗重跑即可,已导出的段会自动跳过。

六、本说明由 NVR 录像导出工具自动生成
=================================================="""

ABOUT_TEXT = """══════════ NVR 录像导出工具 v2.0 ══════════

【支持的设备】
  大华 NVR —— 已实测(v5 固件,5.001 build 2026-03-31),HTTP CGI + Digest
  海康 NVR —— ⚠️ 未经实测,按官方 ISAPI 规范实现,首次使用请先小批量验证
  连接后自动识别品牌;文件名实时取自 NVR 通道表

【环境说明(成功的前提)】
  1 运行电脑与 NVR 网络互通:能访问 NVR 的 IP,HTTP(80)端口可达;
    跨网段时需路由放通
  2 账号有效:用户名/密码正确,未被锁定,且具备「远程回放/下载」权限
  3 NVR 已开启 HTTP 服务(大华默认 80;海康默认 80,ISAPI 默认开启)
  4 所选时间窗内确实有录像:相机在线、录像计划已配置、通道已启用
  5 目标盘剩余空间 ≥ 预检显示的总大小
  6 导出中断不要紧:重新勾选同样通道/时间窗重跑,自动断点续传

【大华 NVR 配置要点(已实测)】
  1 通道管理:确认相机「在线」、录像计划已配置(默认一般已配好)
  2 账号权限:账号需启用「远程:预览/回放/下载」权限
  3 网络服务:确认「HTTP 服务」开启且端口正确(改过端口则地址写 ip:端口)
  4 认证:走 HTTP Digest,无需修改默认安全配置
  5 动检录像的通道只在画面有移动时产生录像,属正常机制

【海康 NVR 配置要点(⚠️ 未经实测,按官方 ISAPI 文档)】
  1 网络:确认 HTTP 端口可达;固件若开启「安全认证/签名」导致 401/403,
    到 系统维护→安全→安全服务 里启用「摘要认证」或临时关闭安全校验
  2 账号权限:勾选「远程:获取参数/回放/下载」权限
  3 通道:确认 IP 通道已添加且在线(工具读 InputProxy 通道表)
  4 导出文件为 .mp4(PS 流),VLC / 海康 iVMS-4200 可播放

【工作原理(视频获取方式)】
  大华:通道表 configManager.cgi → 段枚举 mediaFileFind 两步查询
  (findFile→findNextFile)→ 逐段 RPC_Loadfile 原始 .dav(零转码)→
  每段验「DHII」头与长度;注意媒体接口按 1 基通道号寻址,引擎已统一映射
  海康:通道表 /ISAPI/ContentMgmt/InputProxy/channels → 录像搜索
  POST /ISAPI/ContentMgmt/search(trackID=通道号×100+1 主码流)→
  GET /ISAPI/ContentMgmt/download?playbackURI=… 逐段下载 → 验 PS 头
  通用:长连接复用(Digest 只握手一次)、每 NVR 并发限流 ≤4(调大会
  打崩 NVR 磁盘 IO)、.part 临时文件校验后改名(断点续传)

【导出失败排查(按现象)】
  1 连不上:ping NVR IP;核对网段/路由;账号密码;改过端口写 ip:端口
  2 提示认证失败:密码错或账号被锁(连续错 5 次,等 30 分钟或重启)
  3 检测到 0 通道:账号权限不足,或 HTTP/ISAPI 服务被关闭
  4 预检显示某通道无录像:程序已做二次复检,复检后仍 0 段基本属实;
    换更宽时间窗再确认;相机离线/录像计划未配都会导致
  5 有 OK 也有 FAIL:直接重跑一次,自动续传失败段
  6 速度远低于预期:目标盘写速是瓶颈(移动硬盘 ~25MB/s),避免和其他
    IO 叠加;每 NVR 并发保持 ≤4,调大反而更慢
  7 文件打不开:.dav 用大华 SmartPlayer;.mp4 用 VLC / 海康 iVMS-4200
  8 海康设备异常:确认 ISAPI 已开启、固件支持 ContentMgmt/download;
    因未经实测,请先小批量验证并反馈

【连接配置】
  检测成功后自动记住 NVR 连接,下次启动自动载入并检测;点「重新检测」可修改
  保存在 %APPDATA%\\NVR_Export\\conns.json,密码用 Windows DPAPI 加密,
  只有同一台电脑的同一 Windows 账户能解开;删除该文件即恢复默认
  有 NVR 检测失败时连接框不会收起:请改正密码,避免反复登录失败导致账号被锁

【性能参考】(大华实测 2026-07-12)
  4 台 NVR 聚合 ~50 MB/s(写本机 SSD)/ ~25 MB/s(写 NTFS 移动硬盘)
  490 段 / 163.5 GB 约 107 分钟,0 失败

【开发人员】
  QQ:11680661
══════════════════════════════════════"""

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
WIN_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
DIR_MODES = {"按教室合并": "classroom", "按通道完整名": "full", "全部平铺": "flat"}


# ───────────────────── 连接配置持久化 ─────────────────────
CONF_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "NVR_Export", "conns.json")


def _protect(data, encrypt):
    """Windows 用 DPAPI(CryptProtectData)加/解密:只有同一 Windows 账户能解。
    非 Windows(开发/测试机)退化为原样,仅 base64 —— 不是加密,勿当安全措施。"""
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, dst = Blob(len(data), buf), Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):   # 0x1 = UI_FORBIDDEN
        raise OSError("DPAPI 失败")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def save_conns(conns, path=None):
    """保存连接列表;密码经 DPAPI 加密后 base64,不明文落盘。"""
    path = path or CONF_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [{"ip": c["ip"], "user": c["user"],
             "pass": base64.b64encode(_protect(c["pass"].encode("utf-8"), True)).decode("ascii")}
            for c in conns]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "conns": rows}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                       # 原子替换,写一半断电不会毁掉旧配置


def load_conns(path=None):
    """读取已保存的连接;文件不存在/损坏/换了 Windows 账户解不开 → 返回 []。"""
    try:
        with open(path or CONF_PATH, encoding="utf-8") as f:
            rows = json.load(f)["conns"]
        return [{"ip": r["ip"], "user": r["user"],
                 "pass": _protect(base64.b64decode(r["pass"]), False).decode("utf-8")} for r in rows]
    except (OSError, ValueError, KeyError, TypeError):
        return []


# ───────────────────── 纯逻辑(可单测) ─────────────────────
def parse_conn(text):
    """每行一台 NVR:"地址,账号,密码" → (连接列表, 错误列表)。兼容全角逗号与首尾空白。"""
    conns, errs, seen = [], [], set()
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip().replace("，", ",")
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3 or not IP_RE.match(parts[0]) or not parts[1]:
            errs.append("第%d行(应为 NVR地址,账号,密码):%s" % (i, line))
            continue
        ip, user, pw = parts
        if ip in seen:
            errs.append("第%d行 IP 重复:%s" % (i, ip))
            continue
        seen.add(ip)
        conns.append({"ip": ip, "user": user, "pass": pw})
    return conns, errs


def norm_dt(s):
    """兼容三种写法 → 标准串 "YYYY-MM-DD HH:MM:SS";不合法返回 None。
    支持:"2026-09-05 12:00:00" / 简写 "202609051200"(年月日时分) / "20260905120030"。"""
    s = (s or "").strip()
    if WIN_RE.match(s):
        return s
    if re.match(r"^\d{12}$", s):
        return "%s-%s-%s %s:%s:00" % (s[:4], s[4:6], s[6:8], s[8:10], s[10:12])
    if re.match(r"^\d{14}$", s):
        return "%s-%s-%s %s:%s:%s" % (s[:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])
    return None


def parse_windows(rows):
    """rows=[(start,end)] → (合法窗列表 或 None, 错误消息)。跳过全空行;校验格式/起止/重叠。"""
    wins = []
    for s, e in rows:
        if not s and not e:
            continue
        s, e = norm_dt(s), norm_dt(e)
        if not (s and e):
            return None, '时间格式应为 "2026-09-05 12:00:00" 或简写 "202609051200"(年月日时分):%s ~ %s' % (s or "(空)", e or "(空)")
        try:
            s_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            e_dt = datetime.strptime(e, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None, "日期时间不合法(检查月/日/时/分):%s ~ %s" % (s, e)
        if s_dt >= e_dt:
            return None, "时间窗起止颠倒:%s ~ %s" % (s, e)
        for p0, p1 in wins:
            if s_dt < p1 and e_dt > p0:
                return None, "时间窗存在重叠:%s ~ %s" % (s, e)
        wins.append((s_dt, e_dt))
    if not wins:
        return None, "至少填写一个完整时间窗"
    return [(s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")) for s, e in wins], ""


def cam_dir_for(out, name, mode):
    """目录命名(与 CLI 完全一致):classroom=通道名截到"教室";full=完整通道名;flat=平铺"""
    if mode == "flat":
        return out
    if mode == "classroom" and "教室" in name:
        return os.path.join(out, nd.safe_name(name.split("教室")[0] + "教室"))
    return os.path.join(out, nd.safe_name(name))


def fmt_ts(sec):
    sec = max(int(sec), 0)
    return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def fmt_gb(b):
    return ("%.1f GB" % (b / 1e9)) if b >= 1e9 else ("%.1f MB" % (b / 1e6))


# ───────────────────── 后台导出线程 ─────────────────────
class Exporter(threading.Thread):
    """枚举→预检→(用户确认后)下载。事件进 ev 队列;实时进度写 self.stats。"""

    def __init__(self, cfg, ev):
        super().__init__(daemon=True)
        self.cfg, self.ev = cfg, ev
        self.dbg = bool(cfg.get("debug", False))
        self.ips = list(cfg["ips"])
        self.stop_evt = threading.Event()
        self.confirm_evt = threading.Event()
        self.cancel_evt = threading.Event()
        self.lock = threading.Lock()
        self.stats = {"seg_done": 0, "seg_total": 0, "bytes_done": 0, "bytes_skipped": 0,
                      "bytes_total": 0, "t0": None, "phase": "枚举中", "ok": 0, "fail": 0, "warn": 0}

    def _log(self, m):
        self.ev.put(("log", m))

    def _on_chunk(self, n):
        with self.lock:
            self.stats["bytes_done"] += n

    # ---- 阶段 1:枚举(与 CLI 同逻辑:两步查询/子码流排除/重试) ----
    def _enumerate(self):
        cfg = self.cfg
        auths = {c["ip"]: HTTPDigestAuth(c["user"], c["pass"]) for c in cfg["conns"]}
        self.jobs, self.zero_ch, self.nvr_err = [], [], []

        def enum_nvr(ip):
            eng = self.cfg.get("engines", {}).get(ip, nd)
            try:
                titles = eng.get_channel_titles(ip, auths[ip])
            except Exception as e:
                return ip, [], f"{type(e).__name__}: {e}"
            out = []
            for ch in sorted(cfg["select"].get(ip, ())):
                name = titles.get(ch) or ("通道%d" % ch)
                segs_all = []
                for w0, w1 in cfg["windows"]:
                    segs = None
                    for attempt in range(3):
                        try:
                            segs = eng.find_segments(ip, auths[ip], ch, w0, w1)
                            break
                        except Exception as e:
                            if attempt == 2:
                                self._log("!! %s ch%s %s 枚举失败: %s" % (ip, ch, name, e))
                            else:
                                time.sleep(1)
                    if segs:
                        segs_all.extend(segs)
                if self.dbg:
                    self.ev.put(("log", "[DBG] %s 槽位%d(媒体%d) %s → %d 段"
                                 % (ip, ch, ch + 1, name, len(segs_all))))
                out.append((ch, name, segs_all))
            return ip, out, ""

        with ThreadPoolExecutor(max_workers=min(len(self.ips), 8)) as ex:
            results = list(ex.map(enum_nvr, self.ips))

        # 二次复检:findFile 偶发返回空(NVR 侧搜索抖动,实测 50.2 连续录像通道被误报 0 段),
        # 对第一轮 0 段通道整体重查一遍,防误报"无录像"
        zero_all = [(ip, ch) for ip, chans, _e in results for ch, _n, segs in chans if not segs]
        if zero_all:
            self._log("复检 %d 个 0 段通道(NVR 搜索偶发空结果)…" % len(zero_all))

            def recheck(t):
                ip, ch = t
                got = []
                for w0, w1 in cfg["windows"]:
                    try:
                        got.extend(nd.find_segments(ip, auths[ip], ch, w0, w1))
                    except Exception:
                        pass
                return t, got
            with ThreadPoolExecutor(max_workers=8) as ex2:
                fixed = dict(ex2.map(recheck, zero_all))
            n_fix = sum(1 for v in fixed.values() if v)
            if n_fix:
                self._log("复检纠正:%d 个通道补回录像" % n_fix)
            results = [(ip, [(ch, name, segs + fixed.get((ip, ch), []))
                             for ch, name, segs in chans], err)
                       for ip, chans, err in results]

        seen, skipped = set(), []
        for ip, chans, err in results:
            if err:
                self.nvr_err.append((ip, err))
                self._log("!! %s 检测失败: %s" % (ip, err))
                continue
            n_seg = 0
            for ch, name, segs in chans:
                n_seg += len(segs)
                cdir = cam_dir_for(cfg["out"], name, cfg["dir_mode"])
                for seg in segs:
                    op = os.path.join(cdir, nd.out_name(name, seg))
                    if op in seen:
                        continue
                    seen.add(op)
                    if os.path.exists(op):
                        skipped.append((ip, seg, op))
                    else:
                        self.jobs.append((ip, ch, name, seg, op))
                if not segs:
                    self.zero_ch.append((ip, ch, name))
            self._log("%s: %d 通道 / %d 段" % (ip, len(chans), n_seg))

        def blen(seg):
            return int(seg.get("Length", 0) or 0)
        new_bytes = sum(blen(j[3]) for j in self.jobs)
        skip_bytes = sum(blen(s[1]) for s in skipped)
        with self.lock:
            self.stats["seg_total"] = len(self.jobs) + len(skipped)
            self.stats["seg_done"] = len(skipped)
            self.stats["bytes_total"] = new_bytes + skip_bytes
            self.stats["bytes_skipped"] = skip_bytes
            self.stats["phase"] = "预检完成,等待确认"
        self.skipped_n = len(skipped)
        self.new_bytes = new_bytes

    def _report(self):
        out = self.cfg["out"]
        os.makedirs(out, exist_ok=True)
        try:
            free = shutil.disk_usage(out).free
        except OSError:
            free = None
        est = self.new_bytes / EST_SPEED if self.new_bytes else 0
        return {"out": out, "jobs": len(self.jobs), "skipped": self.skipped_n,
                "new_bytes": self.new_bytes,
                "all_bytes": self.new_bytes + self.stats["bytes_skipped"],
                "est_sec": est, "free": free, "zero_ch": self.zero_ch,
                "nvr_err": self.nvr_err, "empty": not (self.jobs or self.skipped_n)}

    # ---- 阶段 2:下载(每 NVR 限流 + 断点续传 + 实时进度) ----
    def _download(self):
        cfg = self.cfg
        auths = {c["ip"]: HTTPDigestAuth(c["user"], c["pass"]) for c in cfg["conns"]}
        with self.lock:
            self.stats["phase"] = "下载中"
            self.stats["t0"] = time.time()
        self._log("开始下载 %d 段 / %s(并发 %d/NVR)" % (len(self.jobs), fmt_gb(self.new_bytes), cfg["per_nvr"]))
        sem = {ip: threading.Semaphore(cfg["per_nvr"]) for ip in self.ips}

        def run_job(job):
            if self.stop_evt.is_set():
                return
            ip, ch, name, seg, op = job
            os.makedirs(os.path.dirname(op), exist_ok=True)
            with sem[ip]:
                if self.stop_evt.is_set():
                    return
                eng = self.cfg.get("engines", {}).get(ip, nd)
                ok, size, dt, err = eng.download_segment(ip, auths[ip], seg, op, on_chunk=self._on_chunk)
            with self.lock:
                self.stats["seg_done"] += 1
                if ok:
                    self.stats["ok"] += 1
                    if err.startswith("!!"):
                        self.stats["warn"] += 1
                else:
                    self.stats["fail"] += 1
            tag = "OK  " if ok else "FAIL"
            self.ev.put(("log", "%s [%d/%d] %s ch%s %s %.0fMB %.1fs%s"
                         % (tag, self.stats["seg_done"], self.stats["seg_total"], ip, ch,
                            os.path.basename(op), size / 1e6, dt, (" | " + err) if err else "")))

        workers = min(cfg["per_nvr"] * len(self.ips), 32)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(run_job, self.jobs))
        with self.lock:
            st = dict(self.stats)
        if self.stop_evt.is_set() and st["fail"] == 0 and st["ok"] + self.skipped_n < st["seg_total"]:
            with self.lock:
                self.stats["phase"] = "已停止(可重开续传)"
        else:
            with self.lock:
                self.stats["phase"] = "完成"

    def run(self):
        try:
            self._log("枚举 %d 台 NVR × %d 时间窗…" % (len(self.ips), len(self.cfg["windows"])))
            self._enumerate()
            rep = self._report()
            self.ev.put(("precheck", rep))
            if rep["empty"]:
                self.ev.put(("done", {"empty": True}))
                return
            while not (self.confirm_evt.is_set() or self.cancel_evt.is_set() or self.stop_evt.is_set()):
                time.sleep(0.2)
            if not self.confirm_evt.is_set() or self.stop_evt.is_set():
                with self.lock:
                    self.stats["phase"] = "已取消"
                self.ev.put(("done", {"cancelled": True}))
                return
            # 写说明.txt(导出一开始就放,避免中断后目录无说明;已存在不覆盖)
            rp = os.path.join(self.cfg["out"], README_NAME)
            if not os.path.exists(rp):
                with open(rp, "w", encoding="utf-8-sig") as f:
                    f.write(README_TEXT)
                self._log("已写入 %s" % README_NAME)
            self._download()
            with self.lock:
                st = dict(self.stats)
            self.ev.put(("done", {"cancelled": False, "ok": st["ok"], "fail": st["fail"], "warn": st["warn"],
                                  "skipped": self.skipped_n, "bytes": st["bytes_done"],
                                  "elapsed": time.time() - st["t0"] if st["t0"] else 0,
                                  "stopped": self.stop_evt.is_set(), "out": self.cfg["out"]}))
        except Exception as e:
            traceback.print_exc()
            if self.dbg:
                self.ev.put(("log", "[DBG] 导出线程异常:\n" + traceback.format_exc()[-900:]))
            self.ev.put(("done", {"error": "%s: %s" % (type(e).__name__, e)}))


# ───────────────────── 时间窗选择器(纯标准库) ─────────────────────
class DTPicker(tk.Toplevel):
    """年/月/日 + 时/分 两栏(起/止),确定后回填窗行;秒固定 00。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("选择时间窗")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        now = datetime.now()
        pad = {"padx": 3, "pady": 3}
        self.picks = {}
        fields = (("年", [str(now.year), str(now.year + 1)], 7),
                  ("月", ["%02d" % m for m in range(1, 13)], 5),
                  ("日", ["%02d" % d for d in range(1, 32)], 5),
                  ("时", ["%02d" % h for h in range(24)], 5),
                  ("分", ["%02d" % m for m in range(60)], 5))
        defaults = {"年": 0, "月": now.month - 1, "日": now.day - 1, "时": 8, "分": 30}
        for r0, (lab, dh, dm) in enumerate((("起", 8, 30), ("止", 11, 0))):
            ttk.Label(self, text=lab + ":").grid(row=r0, column=0, **pad)
            for c0, (nm, vals, w) in enumerate(fields):
                ttk.Label(self, text=nm).grid(row=r0, column=c0 * 2 + 1, **pad)
                cb = ttk.Combobox(self, values=vals, width=w, state="readonly")
                cb.grid(row=r0, column=c0 * 2 + 2, **pad)
                cb.current(dh if nm == "时" else (dm if nm == "分" else defaults[nm]))
                self.picks[(lab, nm)] = cb
        bf = ttk.Frame(self)
        bf.grid(row=2, column=0, columnspan=12, pady=6)
        ttk.Button(bf, text="确定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bf, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _val(self, lab, nm):
        return int(self.picks[(lab, nm)].get())

    def _ok(self):
        try:
            s = "%04d-%02d-%02d %02d:%02d:00" % (self._val("起", "年"), self._val("起", "月"), self._val("起", "日"),
                                                 self._val("起", "时"), self._val("起", "分"))
            e = "%04d-%02d-%02d %02d:%02d:00" % (self._val("止", "年"), self._val("止", "月"), self._val("止", "日"),
                                                 self._val("止", "时"), self._val("止", "分"))
            datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            datetime.strptime(e, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            messagebox.showwarning("日期不合法", "该月没有这一天,请检查(如 2 月 30 日)", parent=self)
            return
        if s >= e:
            messagebox.showwarning("起止颠倒", "开始时间需早于结束时间", parent=self)
            return
        self.result = (s, e)
        self.destroy()


# ───────────────────── GUI ─────────────────────
class App:
    def __init__(self, root):
        self.root = root
        root.title("NVR 录像导出工具 v2.9")
        root.geometry("1120x700")
        root.minsize(1000, 620)
        self.dark = False                        # 默认浅色;顶栏可切换暗色
        self.q = queue.Queue()
        self.debug = False                      # 调试日志默认关;出问题再开
        self.engines = {}                       # ip → 协议引擎(nd 大华 / hkd 海康)
        self.worker = None
        self.win_rows = []
        self.last_zero = []                     # 最近预检的缺录像通道 [(ip, 槽位, 名称)]
        self.last_export = None                 # 最近一次导出结果(侧栏展示)
        self._chan_cache = []
        self._nvr = {}                          # ip → 分组行 iid
        self._parent_of = {}                    # 通道 iid → NVR 分组 iid
        self._log_open = False
        self._pending_conns = []                # 本次检测所用连接,检测成功后保存
        self._build()
        self._apply_theme(False)                # 默认浅色;顶栏按钮可切换暗色
        root.after(150, self._poll)             # 后台事件唯一消费入口(丢失=界面永远无反应)

    # ---- 主题(深/浅) ----
    THEMES = {
        True: dict(bg="#26282d", panel="#31343a", panel2="#3b3f46", fg="#e8eaed", fg_dim="#a2a8b0",
                   accent="#6ea8ff", accent2="#e0a63f", entry="#22242a", border="#4a4f58",
                   row="#31343a", sel="#3a5a8e", err="#ef6a6a"),
        False: dict(bg="#eef1f4", panel="#ffffff", panel2="#e9edf2", fg="#20242a", fg_dim="#68727c",
                    accent="#2563eb", accent2="#b06010", entry="#ffffff", border="#c9d1d9",
                    row="#ffffff", sel="#cfe0ff", err="#c93c3c"),
    }

    def _apply_theme(self, dark=None):
        if dark is None:
            self.dark = not self.dark
        else:
            self.dark = dark
        C = self.C = self.THEMES[self.dark]
        self.root.configure(bg=C["bg"])
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=C["panel"], foreground=C["fg"], fieldbackground=C["entry"],
                     bordercolor=C["border"], lightcolor=C["panel"], darkcolor=C["panel"],
                     troughcolor=C["entry"], selectbackground=C["sel"], selectforeground=C["fg"],
                     insertcolor=C["fg"], font=("Microsoft YaHei UI", 9))
        st.configure("TLabel", background=C["panel"], foreground=C["fg"])
        st.configure("TFrame", background=C["panel"])
        st.configure("TButton", background=C["panel2"], foreground=C["fg"], bordercolor=C["border"], focusthickness=1)
        st.map("TButton", background=[("active", C["sel"]), ("disabled", C["panel"])],
               foreground=[("disabled", C["fg_dim"])])
        st.configure("TLabelframe", background=C["panel"], foreground=C["accent"], bordercolor=C["border"])
        st.configure("TLabelframe.Label", background=C["panel"], foreground=C["accent"],
                     font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Treeview", background=C["row"], fieldbackground=C["row"], foreground=C["fg"],
                     rowheight=22, bordercolor=C["border"])
        st.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["fg"])])
        st.configure("Treeview.Heading", background=C["panel2"], foreground=C["fg"], relief="flat")
        st.map("Treeview.Heading", background=[("active", C["sel"])])
        st.configure("TProgressbar", troughcolor=C["entry"], background=C["accent"], bordercolor=C["entry"])
        st.configure("TCombobox", fieldbackground=C["entry"], background=C["panel2"],
                     foreground=C["fg"], arrowcolor=C["fg"], bordercolor=C["border"])
        st.map("TCombobox", fieldbackground=[("readonly", C["entry"])],
               background=[("readonly", C["entry"])], foreground=[("readonly", C["fg"])])
        st.configure("TSpinbox", fieldbackground=C["entry"], background=C["panel2"],
                     foreground=C["fg"], arrowcolor=C["fg"], bordercolor=C["border"])
        self.root.option_add("*TCombobox*Listbox.background", C["entry"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["sel"])
        for w in getattr(self, "_texts", []):
            w.configure(bg=C["entry"], fg=C["fg"], insertbackground=C["fg"])
        if hasattr(self, "txt_sum"):
            self._sum_tags()
        if hasattr(self, "btn_theme"):
            self.btn_theme.config(text="☀ 浅色" if self.dark else "🌙 深色")

    def _sum_tags(self):
        C = self.C
        self.txt_sum.configure(bg=C["entry"], fg=C["fg"], insertbackground=C["fg"])
        self.txt_sum.tag_configure("h", font=("Microsoft YaHei UI", 9, "bold"), foreground=C["accent"])
        self.txt_sum.tag_configure("hl", font=("Microsoft YaHei UI", 10, "bold"), foreground=C["accent2"])
        self.txt_sum.tag_configure("k", foreground=C["fg_dim"])
        self.txt_sum.tag_configure("v", foreground=C["fg"])
        self.txt_sum.tag_configure("bad", foreground=C["err"])

    # ---- 控件(通道表为主角;连接/日志可收) ----
    def _build(self):
        p = {"padx": 5, "pady": 3}
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        hdr = ttk.Frame(self.root)
        hdr.grid(row=0, column=0, sticky="ew", padx=5, pady=(4, 0))
        ttk.Label(hdr, text="NVR 录像导出").pack(side="left")
        self.lbl_state = ttk.Label(hdr, text="先检测通道,再勾选")
        self.lbl_state.pack(side="left", padx=12)
        ttk.Button(hdr, text="软件说明", width=8, command=self.on_about).pack(side="right")
        self.btn_theme = ttk.Button(hdr, text="🌙 深色", width=8, command=lambda: self._apply_theme())
        self.btn_theme.pack(side="right", padx=(0, 4))

        f1 = ttk.LabelFrame(self.root, text=" NVR 连接(每行一台:地址,账号,密码) ")
        f1.grid(row=1, column=0, sticky="ew", **p)

        self.f_conn_edit = ttk.Frame(f1)
        self.f_conn_edit.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(0, weight=1)
        self.txt_conn = tk.Text(self.f_conn_edit, width=52, height=3, font=("Consolas", 9),
                                wrap="none", relief="flat", bd=0)
        self.txt_conn.pack(side="left", padx=(4, 8), pady=3)
        saved = load_conns()
        self.txt_conn.insert("1.0", "\n".join("%(ip)s,%(user)s,%(pass)s" % c for c in saved) if saved else DEFAULT_CONNS)
        if saved:                                   # 有保存的配置:启动即自动检测,省一次点击
            self.root.after(300, self.on_detect)
        rbt = ttk.Frame(self.f_conn_edit)
        rbt.pack(side="left", fill="y")
        self.btn_detect = ttk.Button(rbt, text="检测通道", width=10, command=self.on_detect)
        self.btn_detect.pack(pady=(2, 2))
        ttk.Label(rbt, text="一行一台").pack()

        self.f_conn_bar = ttk.Frame(f1)
        self.lbl_conn_bar = ttk.Label(self.f_conn_bar, text="")
        self.lbl_conn_bar.pack(side="left", padx=6)
        ttk.Button(self.f_conn_bar, text="重新检测", width=10,
                   command=self._expand_conn).pack(side="right", padx=6, pady=3)

        body = ttk.Frame(self.root)
        body.grid(row=2, column=0, sticky="nsew", **p)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        f2 = ttk.LabelFrame(body, text=" 通道(按 NVR 分组;单击第一列勾选,点分组勾整台) ")
        f2.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        f2.rowconfigure(0, weight=1)
        f2.columnconfigure(0, weight=1)
        cols = ("sel", "ch", "name")
        self.tree = ttk.Treeview(f2, columns=cols, show="tree headings", height=16)
        self._head = {"sel": "勾", "ch": "通道", "name": "通道名"}
        self.tree.heading("#0", text="NVR")
        self.tree.column("#0", width=118, stretch=False)
        for c, w, t in (("sel", 36, "勾"), ("ch", 48, "通道"), ("name", 220, "通道名")):
            self.tree.heading(c, text=t, command=lambda cc=c: self._sort_by(cc))
            self.tree.column(c, width=w, anchor="center" if c != "name" else "w", stretch=(c == "name"))
        self._sort_col, self._sort_desc = None, False
        self._all = []
        vs = ttk.Scrollbar(f2, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, columnspan=7, sticky="nsew", padx=(3, 0), pady=(2, 0))
        vs.grid(row=0, column=7, sticky="ns", padx=(0, 3), pady=(2, 0))
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<space>", self._tree_space)
        ttk.Button(f2, text="全选", width=6, command=lambda: self._select_all(True)).grid(row=1, column=0, padx=3, pady=2)
        ttk.Button(f2, text="全不选", width=6, command=lambda: self._select_all(False)).grid(row=1, column=1, padx=3, pady=2)
        ttk.Label(f2, text="筛选:").grid(row=1, column=2, sticky="e")
        self.ent_match = ttk.Entry(f2, width=12)
        self.ent_match.grid(row=1, column=3, sticky="w", pady=2)
        self.ent_match.bind("<KeyRelease>", lambda _e: self._apply_filter())
        ttk.Button(f2, text="选可见", width=7, command=self.on_check_visible).grid(row=1, column=4, padx=3, pady=2)
        self.lbl_cnt = ttk.Label(f2, text="已勾选 0/0")
        self.lbl_cnt.grid(row=1, column=5, sticky="w", padx=4, pady=2)
        self.var_slots = tk.BooleanVar(value=False)
        ttk.Checkbutton(f2, text="未命名槽位", variable=self.var_slots,
                        command=lambda: self._fill_channels(self._chan_cache)).grid(row=1, column=6, padx=4, pady=2)

        job = ttk.LabelFrame(body, text=" 作业 ")
        job.grid(row=0, column=1, sticky="ns")
        ttk.Label(job, text="时间窗").grid(row=0, column=0, sticky="w", padx=6, pady=(6, 0))
        self.fw = ttk.Frame(job)
        self.fw.grid(row=1, column=0, sticky="w", padx=4)
        self._add_win_row()
        wf = ttk.Frame(job)
        wf.grid(row=2, column=0, sticky="w", padx=4, pady=(0, 4))
        ttk.Button(wf, text="+ 窗", width=5, command=self.on_add_win).pack(side="left")
        ttk.Button(wf, text="- 删", width=5, command=self.on_del_win).pack(side="left", padx=3)
        ttk.Label(wf, text='简写 202609051200').pack(side="left")
        ttk.Label(job, text="目标目录").grid(row=3, column=0, sticky="w", padx=6)
        pf = ttk.Frame(job)
        pf.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 4))
        job.columnconfigure(0, weight=1)
        self.ent_out = ttk.Entry(pf, width=28)
        self.ent_out.pack(side="left", fill="x", expand=True)
        self.ent_out.bind("<KeyRelease>", lambda _e: self._refresh_summary())
        ttk.Button(pf, text="浏览", width=5, command=self.on_browse).pack(side="left", padx=3)
        opt = ttk.Frame(job)
        opt.grid(row=5, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(opt, text="命名").pack(side="left")
        self.cmb_dir = ttk.Combobox(opt, values=list(DIR_MODES), state="readonly", width=10)
        self.cmb_dir.current(0)
        self.cmb_dir.pack(side="left", padx=4)
        self.cmb_dir.bind("<<ComboboxSelected>>", lambda _e: self._refresh_summary())
        ttk.Label(opt, text="并发").pack(side="left", padx=(8, 2))
        self.spn_conc = ttk.Spinbox(opt, from_=1, to=4, width=3, command=self._refresh_summary)
        self.spn_conc.set(4)
        self.spn_conc.pack(side="left")
        self.spn_conc.bind("<KeyRelease>", lambda _e: self._refresh_summary())
        bf = ttk.Frame(job)
        bf.grid(row=6, column=0, sticky="ew", padx=6, pady=(4, 2))
        self.btn_go = ttk.Button(bf, text="预检并导出", width=12, command=self.on_go)
        self.btn_go.pack(side="left")
        self.btn_stop = ttk.Button(bf, text="停止", width=7, command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.pb = ttk.Progressbar(job, maximum=1, value=0)
        self.pb.grid(row=7, column=0, sticky="ew", padx=6, pady=2)
        self.lbl_stat = ttk.Label(job, text="待机")
        self.lbl_stat.grid(row=8, column=0, sticky="w", padx=6)
        self.txt_sum = tk.Text(job, width=36, height=14, state="disabled", wrap="char",
                               font=("Microsoft YaHei UI", 9), relief="flat", bd=0)
        self.txt_sum.grid(row=9, column=0, sticky="nsew", padx=6, pady=(4, 6))
        job.rowconfigure(9, weight=1)

        f5 = ttk.Frame(self.root)
        f5.grid(row=3, column=0, sticky="ew", **p)
        self.btn_log = ttk.Button(f5, text="日志 ▸", width=8, command=self._toggle_log)
        self.btn_log.pack(side="left")
        self.var_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(f5, text="调试日志", variable=self.var_debug,
                        command=self.on_debug_toggle).pack(side="left", padx=8)
        self.txt = tk.Text(f5, height=2, state="disabled", wrap="none", font=("Consolas", 9), relief="flat", bd=0)
        ys = ttk.Scrollbar(f5, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=ys.set)
        self.txt.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=2)
        ys.pack(side="right", fill="y", pady=2)

        self._texts = [self.txt_conn, self.txt, self.txt_sum]

    # ---- 时间窗行 ----
    def _add_win_row(self):
        row = ttk.Frame(self.fw)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="窗%d 起:" % (len(self.win_rows) + 1)).pack(side="left")
        s = ttk.Entry(row, width=21)
        s.pack(side="left", padx=2)
        ttk.Label(row, text="止:").pack(side="left")
        e = ttk.Entry(row, width=21)
        e.pack(side="left", padx=2)
        for w0 in (s, e):
            w0.bind("<KeyRelease>", lambda _e: self._refresh_summary())
        ttk.Button(row, text="选择…", command=lambda r=len(self.win_rows): self.on_pick_time(r)).pack(side="left", padx=2)
        self.win_rows.append({"frame": row, "start": s, "end": e})

    # ---- 通道树 ----
    def _is_nvr(self, iid):
        return iid in self._nvr.values()

    def _tree_click(self, e):
        iid = self.tree.identify_row(e.y)
        col = self.tree.identify_column(e.x)
        if iid and col == "#1":
            self._toggle(iid)

    def _tree_space(self, _e):
        for iid in self.tree.selection():
            self._toggle(iid)

    def _toggle(self, iid):
        if self._is_nvr(iid):
            kids = [k for k in self._all if self.tree.parent(k) == iid]
            on = not all(self.tree.item(k, "values")[0] == "☑" for k in kids) if kids else True
            for k in kids:
                v = list(self.tree.item(k, "values"))
                v[0] = "☑" if on else "☐"
                self.tree.item(k, values=v)
            self._sync_nvr(iid)
            self._update_counts()
            return
        v = list(self.tree.item(iid, "values"))
        v[0] = "☐" if v[0] == "☑" else "☑"
        self.tree.item(iid, values=v)
        self._sync_nvr(self.tree.parent(iid))
        self._update_counts()

    def _sync_nvr(self, nid):
        if not nid:
            return
        kids = [k for k in self._all if self.tree.parent(k) == nid]
        n = sum(1 for k in kids if self.tree.item(k, "values")[0] == "☑")
        mark = "☑" if kids and n == len(kids) else ("☐" if n == 0 else "▣")
        self.tree.item(nid, values=(mark, "", "%d/%d" % (n, len(kids))))

    def _select_all(self, on):
        for iid in self._all:
            v = list(self.tree.item(iid, "values"))
            v[0] = "☑" if on else "☐"
            self.tree.item(iid, values=v)
        for nid in self._nvr.values():
            self._sync_nvr(nid)
        self._update_counts()

    def _selected(self):
        sel = {}
        for iid in self._all:
            v = self.tree.item(iid, "values")
            if v[0] == "☑":
                ip = self.tree.parent(iid)
                ip = self.tree.item(ip, "text") if ip else ""
                sel.setdefault(ip, set()).add(int(v[1]) - 1)
        return sel

    def _fill_channels(self, results):
        self._chan_cache = results or []
        show_slots = self.var_slots.get()
        self.tree.delete(*self.tree.get_children())
        self._all, self._nvr, self._parent_of = [], {}, {}
        for ip, titles in sorted(results or [], key=lambda x: x[0]):
            nid = self.tree.insert("", "end", text=ip, values=("☐", "", ""), open=True)
            self._nvr[ip] = nid
            for ch in sorted(titles):
                name = titles[ch] or "(未命名)"
                if not show_slots and (not name or name.startswith("通道")):
                    continue
                iid = self.tree.insert(nid, "end", values=("☐", ch + 1, name))
                self._all.append(iid)
                self._parent_of[iid] = nid
            self._sync_nvr(nid)
        self._sort_col, self._sort_desc = None, False
        self._apply_filter()
        self._update_counts()

    # ---- 排序 / 计数 / 摘要侧栏 ----
    def _sort_by(self, col):
        desc = col == self._sort_col and not self._sort_desc
        self._sort_col, self._sort_desc = col, desc
        idx = {"sel": 0, "ch": 1, "name": 2}[col]

        def key(t):
            v = t[0]
            if col == "ch":
                try:
                    return (0, int(v[1]), "")
                except ValueError:
                    return (1, 0, str(v[1]))
            return (0, 0, str(v[idx]))
        rebuilt = []
        for nid in self.tree.get_children():
            items = [(self.tree.item(i, "values"), i) for i in self.tree.get_children(nid)]
            items.sort(key=key, reverse=desc)
            for pos, (_v, iid) in enumerate(items):
                self.tree.move(iid, nid, pos)
                rebuilt.append(iid)
        hidden = [i for i in self._all if i not in set(rebuilt)]
        self._all = rebuilt + hidden
        for c in self._head:
            self.tree.heading(c, text=self._head[c])
        self.tree.heading(col, text=self._head[col] + (" ▼" if desc else " ▲"))

    def _update_counts(self):
        n = sum(1 for i in self._all if self.tree.item(i, "values")[0] == "☑")
        self.lbl_cnt.config(text="已勾选 %d/%d" % (n, len(self._all)))
        self._refresh_summary()

    @staticmethod
    def _set_text(w, segs):
        content = "".join(t for t, _tag in segs)
        if w.get("1.0", "end").strip() == content.strip():   # 内容未变不重绘,保住滚动位置
            return
        w.config(state="normal")
        w.delete("1.0", "end")
        for text, tag in segs:
            w.insert("end", text, tag)
        w.config(state="disabled")

    def _refresh_summary(self):
        n = sum(1 for i in self._all if self.tree.item(i, "values")[0] == "☑")
        segs = [("◆ 已选  ", "h"), ("%d / %d\n" % (n, len(self._all)), "hl")]
        segs.append(("\n◆ 缺录像\n", "h"))
        if self.last_zero:
            for ip, ch, nm in self.last_zero:
                segs.append(("  %s ch%d %s\n" % (ip, ch + 1, nm), "bad"))
        else:
            segs.append(("  (预检后显示)\n", "k"))
        segs.append(("\n◆ 上次导出\n", "h"))
        le = self.last_export
        if le:
            st = "已停止" if le.get("stopped") else ("有失败" if le.get("fail") else ("有告警" if le.get("warn") else "完成"))
            segs.append(("  %s  OK %d 失败 %d 跳过 %d\n" % (st, le["ok"], le["fail"], le["skipped"]), "v"))
            segs.append(("  %s / %s\n" % (fmt_gb(le["bytes"]), fmt_ts(le["elapsed"])), "k"))
        else:
            segs.append(("  (未导出)\n", "k"))
        self._set_text(self.txt_sum, segs)

    def on_about(self):
        top = tk.Toplevel(self.root)
        top.title("软件说明")
        top.transient(self.root)
        top.grab_set()
        t = tk.Text(top, width=92, height=27, wrap="word", font=("Microsoft YaHei UI", 9),
                    bg="#fbfcfd", relief="flat")
        s = ttk.Scrollbar(top, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=s.set)
        t.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        s.pack(side="right", fill="y", pady=10)
        t.insert("1.0", ABOUT_TEXT)
        t.config(state="disabled")
        ttk.Button(top, text="关闭", command=top.destroy).pack(pady=(0, 10))
        self.root.update_idletasks()
        top.geometry("+%d+%d" % (self.root.winfo_rootx() + 90, self.root.winfo_rooty() + 30))

    # ---- 事件 ----
    def on_detect(self):
        conns, errs = parse_conn(self.txt_conn.get("1.0", "end"))
        if errs:
            messagebox.showwarning("连接格式有误", "请检查第1区连接列表:\n" + "\n".join(errs[:6]))
            return
        if not conns:
            messagebox.showwarning("缺少连接", "请填写 NVR 连接(每行: 地址,账号,密码)")
            return
        self._pending_conns = conns                 # 检测成功后保存
        self.btn_detect.config(state="disabled")
        self.lbl_state.config(text="检测中(并行 %d 台)…" % len(conns))
        self._log("检测 %s …" % ", ".join(c["ip"] for c in conns))

        dbg = self.debug

        def qlog(m):
            if dbg:
                self.q.put(("log", "[DBG] " + m))

        def work():
            ok, err, engines, brands = [], [], {}, {}   # engines 存模块,brands 存展示名
            def one(c):
                ip, auth = c["ip"], HTTPDigestAuth(c["user"], c["pass"])
                qlog("%s: 尝试大华 CGI…" % ip)
                try:                                    # 先按大华探测
                    titles = nd.get_channel_titles(ip, auth)
                    if titles:
                        engines[ip] = nd
                        brands[ip] = "大华"
                        qlog("%s: 大华 OK,%d 个通道槽位" % (ip, len(titles)))
                        return (ip, titles), None
                    qlog("%s: 大华返回空表" % ip)
                except Exception as e:
                    qlog("%s: 大华失败 %s" % (ip, e))
                    titles = {}
                qlog("%s: 尝试海康 ISAPI…" % ip)
                try:                                    # 再按海康探测(未经实测)
                    if hkd.is_hikvision(ip, auth):
                        qlog("%s: 海康设备确认" % ip)
                        titles = hkd.get_channel_titles(ip, auth)
                        if titles:
                            engines[ip] = hkd
                            brands[ip] = "海康(未经实测)"
                            qlog("%s: 海康 OK,%d 个通道槽位" % (ip, len(titles)))
                            return (ip, titles), None
                    qlog("%s: 非海康或通道表为空" % ip)
                except Exception as e:
                    qlog("%s: 海康失败 %s" % (ip, e))
                return None, (ip, "无法识别品牌/取不到通道表(查网络、端口、账号及远程回放权限)")
            try:
                with ThreadPoolExecutor(max_workers=min(len(conns), 8)) as ex:
                    for res, er in ex.map(one, conns):
                        (ok.append(res) if res else err.append(er))
                qlog("检测结束:成功 %d,失败 %d" % (len(ok), len(err)))
                self.q.put(("channels", ok, err, engines, brands))
            except Exception:
                err.append(("调度异常", traceback.format_exc()[-300:]))
                self.q.put(("log", "[DBG] 检测线程异常:\n" + traceback.format_exc()[-800:]))
                self.q.put(("channels", ok, err, engines, brands))
        threading.Thread(target=work, daemon=True).start()

    def _apply_filter(self):
        """名称筛选:只显示匹配行(显示层过滤;隐藏行勾选保留,支持反复查找追加)。"""
        q = self.ent_match.get().strip().lower()
        keep = {}
        for iid in self._all:
            nid = self._parent_of.get(iid)
            v = self.tree.item(iid, "values")
            if q and q not in str(v[2]).lower():
                self.tree.detach(iid)
            else:
                keep.setdefault(nid, []).append(iid)
        for nid, kids in keep.items():
            for pos, iid in enumerate(kids):
                self.tree.move(iid, nid, pos)
            self._sync_nvr(nid)

    def on_check_visible(self):
        """勾选当前可见通道(配合筛选:先筛出想要的,再一键勾选)。"""
        n = 0
        vis = set()
        for nid in self.tree.get_children():
            vis.update(self.tree.get_children(nid))
        for iid in vis:
            if iid not in self._all:
                continue
            v = list(self.tree.item(iid, "values"))
            if v[0] != "☑":
                v[0] = "☑"
                self.tree.item(iid, values=v)
                n += 1
        for nid in self._nvr.values():
            self._sync_nvr(nid)
        if n:
            self._log("已勾选当前可见的 %d 个通道" % n)
        self._update_counts()

    def on_pick_time(self, r):
        dlg = DTPicker(self.root)
        self.root.wait_window(dlg)
        if dlg.result:
            row = self.win_rows[r]
            for key, val in (("start", dlg.result[0]), ("end", dlg.result[1])):
                row[key].delete(0, "end")
                row[key].insert(0, val)

    def on_debug_toggle(self):
        self.debug = bool(self.var_debug.get())
        self._log("调试日志:%s" % ("开" if self.debug else "关"))
        if self.debug and not self._log_open:
            self._toggle_log(True)

    def _expand_conn(self):
        self.f_conn_bar.grid_forget()
        self.f_conn_edit.grid(row=0, column=0, sticky="ew")
        self.lbl_state.config(text="重新检测通道")

    def _collapse_conn(self, n_nvr, n_ch):
        ips = "、".join(sorted(self._nvr))
        self.lbl_conn_bar.config(text="已连接 %d 台 / %d 通道  %s" % (n_nvr, n_ch, ips))
        self.f_conn_edit.grid_forget()
        self.f_conn_bar.grid(row=0, column=0, sticky="ew")

    def _toggle_log(self, force=None):
        self._log_open = (not self._log_open) if force is None else bool(force)
        self.txt.config(height=8 if self._log_open else 2)
        self.btn_log.config(text="日志 ▾" if self._log_open else "日志 ▸")

    def on_add_win(self):
        if len(self.win_rows) >= 4:
            return
        self._add_win_row()

    def on_del_win(self):
        if len(self.win_rows) <= 1:
            return
        row = self.win_rows.pop()
        row["frame"].destroy()

    def on_browse(self):
        d = filedialog.askdirectory(parent=self.root, title="选择导出目标目录")
        if d:
            self.ent_out.delete(0, "end")
            self.ent_out.insert(0, os.path.normpath(d))
            self._refresh_summary()

    def _gather_windows(self):
        rows = [(r["start"].get().strip(), r["end"].get().strip()) for r in self.win_rows]
        return parse_windows(rows)

    def on_go(self):
        if self.worker and self.worker.is_alive():
            return
        sel = self._selected()
        if not sel:
            messagebox.showwarning("未选通道", "请先检测并勾选要导出的通道")
            return
        wins, err = self._gather_windows()
        if err:
            messagebox.showwarning("时间窗有误", err)
            return
        out = self.ent_out.get().strip()
        if not out:
            messagebox.showwarning("缺少目录", "请选择导出目标目录")
            return
        conns, errs = parse_conn(self.txt_conn.get("1.0", "end"))
        if errs or not conns:
            messagebox.showwarning("连接有误", "\n".join(errs[:6]) or "请填写连接列表(每行: 地址,账号,密码)")
            return
        missing = [ip for ip in sel if ip not in {c["ip"] for c in conns}]
        if missing:
            messagebox.showwarning("连接与勾选不一致", "勾选的通道属于连接列表中已删除的 NVR:%s" % ", ".join(missing))
            return
        try:
            conc = max(1, min(4, int(self.spn_conc.get())))
        except ValueError:
            conc = 4
        cfg = {"ips": [c["ip"] for c in conns], "conns": conns, "engines": dict(self.engines),
               "debug": self.debug,
               "windows": wins, "select": sel, "out": os.path.normpath(out),
               "dir_mode": DIR_MODES[self.cmb_dir.get()], "per_nvr": conc}
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.pb.config(value=0)
        self.worker = Exporter(cfg, self.q)
        self.worker.start()

    def on_stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_evt.set()
            self.worker.cancel_evt.set()
            self._log("已请求停止:等待在途段完成后收尾(可重开软件续传)")

    # ---- 事件轮询 ----
    def _log(self, m):
        self.txt.config(state="normal")
        self.txt.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), m))
        if int(self.txt.index("end-1c").split(".")[0]) > 3000:
            self.txt.delete("1.0", "1000.0")
        self.txt.see("end")
        self.txt.config(state="disabled")

    def _poll(self):
        try:
            try:
                while True:
                    ev = self.q.get_nowait()
                    kind = ev[0]
                    if kind == "log":
                        self._log(ev[1])
                    elif kind == "channels":
                        ok, err, engines, brands = ev[1], ev[2], ev[3], ev[4]
                        self.engines = dict(engines)   # ip → 引擎模块
                        self._fill_channels(ok)
                        if brands:
                            self._log("品牌识别: %s" % ", ".join("%s=%s" % kv for kv in sorted(brands.items())))
                        for ip, m in err:
                            self._log("!! %s 检测失败: %s" % (ip, m))
                        if ok:
                            n = len(self._all)
                            # 检测期间用户改过连接框:不覆盖保存、不收起,等用户自己再点检测
                            edited = parse_conn(self.txt_conn.get("1.0", "end"))[0] != self._pending_conns
                            if not edited:
                                try:
                                    save_conns(self._pending_conns)
                                    self._log("已保存 NVR 连接配置(下次启动自动载入):%s" % CONF_PATH)
                                except Exception as e:      # 保存失败不影响本次使用
                                    self._log("!! 保存连接配置失败:%s" % e)
                            self._log("检测完成:%d 台 / %d 通道,请勾选后填写导出参数" % (len(ok), n))
                            fail = (",%d 台失败(查密码/网络)" % len(err)) if err else ""
                            self.lbl_state.config(text="已检测 %d 台 / %d 通道%s" % (len(ok), n, fail))
                            if not (edited or err):   # 有失败就留着编辑框,免得错密码每次启动都被自动重试
                                self._collapse_conn(len(ok), n)
                        else:
                            self.lbl_state.config(text="检测失败(检查地址/网络/密码)")
                        self.btn_detect.config(state="normal")
                    elif kind == "precheck":
                        self._on_precheck(ev[1])
                    elif kind == "done":
                        self._on_done(ev[1])
            except queue.Empty:
                pass
            self._refresh_progress()
            self._update_counts()   # 摘要常刷:键入/程序写入/弹窗回填都能及时反映
        except Exception:
            # 轮询任何单次出错都不允许杀死事件链(pythonw 下会静默)
            try:
                self._log("[异常] 轮询出错:\n" + traceback.format_exc()[-500:])
            except Exception:
                pass
        self.root.after(150, self._poll)

    def _refresh_progress(self):
        w = self.worker
        if not w:
            return
        with w.lock:
            st = dict(w.stats)
        total = st["bytes_total"]
        done = st["bytes_skipped"] + st["bytes_done"]
        if total:
            self.pb.config(maximum=total, value=min(done, total))
            el = time.time() - st["t0"] if st["t0"] else 0
            sp = st["bytes_done"] / el if el > 0.5 else 0
            eta = (total - done) / sp if sp > 0 else 0
            self.lbl_stat.config(text='%s | %d/%d 段 | %s / %s | %s | 剩余~%s | OK %d / FAIL %d'
                                 % (st["phase"], st["seg_done"], st["seg_total"], fmt_gb(done), fmt_gb(total),
                                    ("%0.1f MB/s" % (sp / 1e6)) if sp else "-", fmt_ts(eta), st["ok"], st["fail"]))
        else:
            self.lbl_stat.config(text=st["phase"])

    def _on_precheck(self, rep):
        self.last_zero = list(rep["zero_ch"])   # 侧栏展示缺录像通道
        top = tk.Toplevel(self.root)
        top.title("预检结果")
        top.transient(self.root)
        top.grab_set()
        lines = []
        if rep["empty"]:
            lines.append("所选通道在所选时间窗内没有录像,无可导出内容。")
        else:
            lines.append("目标目录: %s" % rep["out"])
            lines.append("待下载: %d 段 / %s" % (rep["jobs"], fmt_gb(rep["new_bytes"])))
            if rep["skipped"]:
                lines.append("已存在(断点续传跳过): %d 段 / %s" % (rep["skipped"], fmt_gb(rep["all_bytes"] - rep["new_bytes"])))
            lines.append("预计耗时: ~%s(按 %.0f MB/s 估算)" % (fmt_ts(rep["est_sec"]), EST_SPEED / 1e6))
            if rep["free"] is not None:
                okfree = rep["free"] > rep["new_bytes"] * 1.05
                lines.append("目标盘剩余: %s %s" % (fmt_gb(rep["free"]), "(充足)" if okfree else "(不足,请更换目标盘!)"))
            if rep["zero_ch"]:
                lines.append("")
                lines.append("无录像通道(%d 个,请确认是否接受):" % len(rep["zero_ch"]))
                lines.append("  " + "; ".join("%s ch%s %s" % z for z in rep["zero_ch"][:20]))
            if rep["nvr_err"]:
                lines.append("检测失败 NVR: " + "; ".join("%s %s" % e for e in rep["nvr_err"]))
        t = tk.Text(top, width=78, height=max(10, min(20, len(lines) + 2)), wrap="word")
        t.pack(fill="both", expand=True, padx=10, pady=8)
        for ln in lines:
            t.insert("end", ln + "\n")
        t.config(state="disabled")
        bf = ttk.Frame(top)
        bf.pack(pady=6)

        def do_cancel():
            self.worker.cancel_evt.set()
            top.destroy()
        ttk.Button(bf, text="取消", command=do_cancel).pack(side="left", padx=6)
        if not rep["empty"]:
            def do_go():
                self.worker.confirm_evt.set()
                top.destroy()
            ttk.Button(bf, text="开始导出", command=do_go).pack(side="left", padx=6)
        top.protocol("WM_DELETE_WINDOW", do_cancel)
        self.root.update_idletasks()
        top.geometry("+%d+%d" % (self.root.winfo_rootx() + 60, self.root.winfo_rooty() + 80))

    def _on_done(self, d):
        self.btn_go.config(state="normal")
        self.btn_stop.config(state="disabled")
        if d.get("error"):
            self._log("!! 出错终止: %s" % d["error"])
            messagebox.showerror("导出出错", d["error"])
            return
        if d.get("empty"):
            self._log("无可导出内容")
            return
        if d.get("cancelled"):
            self._log("已取消/停止")
            return
        self.last_export = dict(d)              # 侧栏展示最近导出
        if d.get("warn"):
            self._log("!! %d 段段头残留旧帧未能清理,播放时间可能不对(见上方 !! 行),请抽查" % d["warn"])
            messagebox.showwarning("播放时间告警", "%d 段文件开头的残留旧画面未能清理,\n播放起始时间可能不对,请按日志中 !! 行抽查。" % d["warn"])
        if d.get("fail"):
            self._log("完成但有失败:OK=%d FAIL=%d,建议重新点击[预检并导出]续传失败段" % (d["ok"], d["fail"]))
            messagebox.showwarning("部分失败", "OK=%d,FAIL=%d\n重跑即可自动续传失败段" % (d["ok"], d["fail"]))
        else:
            self._log("完成:OK=%d 已存在=%d %s / %s" % (d["ok"], d["skipped"], fmt_gb(d["bytes"]), fmt_ts(d["elapsed"])))
            if messagebox.askyesno("完成", "导出完成,已在根目录生成说明.txt。\n是否打开导出目录?"):
                try:
                    os.startfile(d["out"])
                except AttributeError:
                    self._log("(当前系统无 os.startfile,请手动打开:%s)" % d["out"])


# ───────────────────── 入口 ─────────────────────
def _smoke():
    """无头自检:构建 UI → 注入假通道 → 校验勾选/时间窗/参数收集。"""
    global CONF_PATH
    import tempfile
    CONF_PATH = os.path.join(tempfile.mkdtemp(), "conns.json")   # 不读写真实配置
    root = tk.Tk()
    app = App(root)
    app._fill_channels([("10.10.50.1", {0: "九教室东", 1: "九教室西", 2: ""}),
                        ("10.10.50.2", {19: "大门口"})])
    app._select_all(True)
    sel = app._selected()
    assert sel == {"10.10.50.1": {0, 1, 2}, "10.10.50.2": {19}}, sel
    app.on_add_win()
    r0, r1 = app.win_rows
    r0["start"].delete(0, "end"); r0["start"].insert(0, "2026-07-12 08:30:00")
    r0["end"].delete(0, "end");   r0["end"].insert(0, "2026-07-12 11:00:00")
    r1["start"].delete(0, "end"); r1["start"].insert(0, "2026-07-12 13:30:00")
    r1["end"].delete(0, "end");   r1["end"].insert(0, "2026-07-12 16:00:00")
    wins, err = app._gather_windows()
    assert not err and len(wins) == 2, (wins, err)
    app.on_add_win()
    app.win_rows[2]["start"].insert(0, "2026-07-12 15:00:00")
    app.win_rows[2]["end"].insert(0, "2026-07-12 17:00:00")
    _, err = app._gather_windows()
    assert err, "重叠窗口应报错"
    app.on_del_win(); app.on_del_win()
    assert len(app.win_rows) == 1
    # v1.1:简写时间 / 名称匹配 / 多行连接
    r0 = app.win_rows[0]
    r0["start"].delete(0, "end"); r0["start"].insert(0, "202609050830")
    r0["end"].delete(0, "end");   r0["end"].insert(0, "202609051100")
    wins2, err2 = app._gather_windows()
    assert not err2 and wins2[0] == ("2026-09-05 08:30:00", "2026-09-05 11:00:00"), (wins2, err2)
    app._select_all(False)
    app.ent_match.insert(0, "教室")
    app._apply_filter()                              # v2.6:筛选=显示过滤,不自动勾选
    sel2 = app._selected()
    assert not sel2, sel2
    app.on_check_visible()
    sel2 = app._selected()
    assert sel2 == {"10.10.50.1": {0, 1}}, sel2   # (未命名)不含"教室",不匹配
    # v1.2:计数 / 排序 / 摘要侧栏
    assert app.lbl_cnt.cget("text") == "已勾选 2/4", app.lbl_cnt.cget("text")
    app.ent_match.delete(0, "end")
    app._apply_filter()                              # 清筛选恢复全量,再验排序
    app._sort_by("name")
    n0 = app.tree.get_children()[0]
    chs = [int(app.tree.item(i, "values")[1]) for i in app.tree.get_children(n0)]
    assert chs[0] == 3, "名称升序(未命名,槽位2→显示3)应排最前: %s" % chs
    app._sort_by("ch")
    n0 = app.tree.get_children()[0]
    chs = [int(app.tree.item(i, "values")[1]) for i in app.tree.get_children(n0)]
    assert chs[0] == 1, "通道号升序应排最前(槽位0→显示1): %s" % chs
    app._sort_by("ch")
    n1 = app.tree.get_children()[1]
    chs = [int(app.tree.item(i, "values")[1]) for i in app.tree.get_children(n1)]
    assert chs[0] == 20, "再点应切换降序(槽位19→显示20)最前: %s" % chs
    sum_txt = app.txt_sum.get("1.0", "end")
    assert "◆ 已选" in sum_txt and "2 / 4" in sum_txt, sum_txt
    assert "◆ 缺录像" in sum_txt and "◆ 上次导出" in sum_txt, sum_txt
    assert len(ABOUT_TEXT) > 400
    app._select_all(True)
    assert app.lbl_cnt.cget("text") == "已勾选 4/4"
    conns3, errs3 = parse_conn(app.txt_conn.get("1.0", "end"))
    assert len(conns3) == 4 and not errs3
    # v2.9:检测成功 → 保存;下次启动 → 连接框载回同样内容
    app._pending_conns = conns3
    app.q.put(("channels", [("10.10.50.1", {0: "九教室东"})], [], {"10.10.50.1": nd}, {"10.10.50.1": "大华"}))
    # 事件队列必须被 _poll 消费(v2.1 曾丢失 after 调度,导致界面永久无反应)
    app.q.put(("log", "POLL_TEST"))
    hit = []
    root.after(400, lambda: hit.append("POLL_TEST" in app.txt.get("1.0", "end")))
    root.after(500, root.destroy)
    root.mainloop()
    assert hit and hit[0], "_poll 未运行:后台队列事件从未被消费"
    assert load_conns() == conns3, "检测成功后应保存连接配置"
    root2 = tk.Tk()
    app2 = App(root2)
    assert parse_conn(app2.txt_conn.get("1.0", "end"))[0] == conns3, "重启后应载回已保存的连接"
    root2.destroy()
    print("SMOKE_OK 通道=%s 匹配=%s 时间窗=%s 轮询=OK" % (sel, sel2, wins2))
    return


def main():
    if "--smoke" in sys.argv:
        _smoke()
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        if getattr(sys, "_MEIPASS", None):
            root.iconbitmap(os.path.join(sys._MEIPASS, "icon.ico"))
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("启动失败", traceback.format_exc()[-1500:])
        except Exception:
            pass
        sys.exit(1)
