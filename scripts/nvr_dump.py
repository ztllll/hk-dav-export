#!/usr/bin/env python3
"""
nvr_dump.py — 大华 NVR 批量原始 .dav 下载(不转码)

用法示例:
  # 下载 4 台 NVR 全部通道某时段
  ./nvr_dump.py --start "2026-07-12 09:00:00" --end "2026-07-12 11:00:00" --out /path/to/dir

  # 只下指定 NVR / 指定通道(通道号或名称子串)
  ./nvr_dump.py --nvr 10.10.50.2 --channels 0-7 --start ... --end ... --out ...
  ./nvr_dump.py --name-filter 九教室 --start ... --end ... --out ...

特性:
  - requests.Session 长连接复用(digest 只握手一次,之后每段无额外开销)
  - 按"段"全局并行,每 NVR 限流(默认 4),多台 NVR 同时打满
  - .part 落盘 → 验 DHII magic + 长度 → rename,可安全中断重跑(自动跳过已下载)
  - 每段失败自动重试(默认 2 次)

协议要点(大华 v5 固件,踩坑结论,勿改):
  - findFile 只返回 OK,真实列表在 findNextFile(必须两步)
  - 条件用 dot-form 独立参数: condition.Channel=N&condition.StartTime=...(空格→%20)
  - .dav 文件头 magic = b"DHII"
  - FilePath 末尾可能带 \r,须去除
  - RPC_Loadfile 不支持 Range 断点续传
"""
import argparse
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPDigestAuth

DEFAULT_NVRS = ["10.10.50.1", "10.10.50.2", "10.10.50.3", "10.10.50.4"]

_tls = threading.local()
_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def session_for(ip, auth):
    """每线程每 IP 一个长连接 Session。"""
    key = f"s_{ip.replace('.', '_')}"
    s = getattr(_tls, key, None)
    if s is None:
        s = requests.Session()
        s.auth = auth
        setattr(_tls, key, s)
    return s


def parse_kv(text):
    """解析 CGI 的 items[N].Key=Value 输出。"""
    items = defaultdict(dict)
    for line in text.splitlines():
        m = re.match(r"^items\[(\d+)\]\.(\w+)(?:\[\d+\])?=(.*)$", line.strip())
        if m:
            items[int(m.group(1))][m.group(2)] = m.group(3).rstrip("\r")
    return [items[i] for i in sorted(items)]


def get_channel_titles(ip, auth):
    """返回 {channel_index: name}。"""
    s = session_for(ip, auth)
    r = s.get(
        f"http://{ip}/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle",
        timeout=10,
    )
    titles = {}
    for line in r.text.splitlines():
        m = re.match(r"^table\.ChannelTitle\[(\d+)\]\.Name=(.*)$", line.strip())
        if m:
            titles[int(m.group(1))] = m.group(2).rstrip("\r")
    return titles


def find_segments(ip, auth, ch, start, end):
    """枚举一个通道时间窗内的所有录像段。

    ch = 通道标题槽位号(table.ChannelTitle 的 0 基下标)。
    大华媒体接口按 1 基通道号寻址:mediaFileFind 的 Channel、snapshot、
    存储目录号均为 槽位号+1(2026-09-07 四台 NVR 实测:查 ch1 → 存储目录
    /0/;媒体 ch0 不存在,快照一律 400)。此处统一 +1 映射,调用方勿重复加。"""
    s = session_for(ip, auth)
    base = f"http://{ip}/cgi-bin/mediaFileFind.cgi"
    r = s.get(f"{base}?action=factory.create", timeout=10)
    m = re.search(r"result=(\d+)", r.text)
    if not m:
        raise RuntimeError(f"factory.create failed: {r.text[:100]}")
    h = m.group(1)
    segs = []
    try:
        q = urllib.parse.quote
        s.get(
            f"{base}?action=findFile&object={h}&condition.Channel={ch + 1}"
            f"&condition.StartTime={q(start)}&condition.EndTime={q(end)}",
            timeout=15,
        )
        while True:
            r = s.get(f"{base}?action=findNextFile&object={h}&count=100", timeout=20)
            batch = parse_kv(r.text)
            m = re.search(r"found=(\d+)", r.text)
            found = int(m.group(1)) if m else 0
            segs.extend(batch)
            if found < 100:
                break
    finally:
        try:
            s.get(f"{base}?action=close&object={h}", timeout=5)
        except requests.RequestException:
            pass
    return [
        x for x in segs
        if x.get("FilePath")
        and x.get("Type", "dav") == "dav"
        and x.get("VideoStream", "Main") == "Main"  # 排除子码流(Extra1),避免同名撞车
    ]


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).replace(" ", "_")


def download_segment(ip, auth, seg, out_path, retries=2, on_chunk=None):
    """下载单段,验 magic+长度,返回 (ok, bytes, seconds, err)。"""
    fp = seg["FilePath"]
    expected = int(seg.get("Length", 0) or 0)
    url = f"http://{ip}/cgi-bin/RPC_Loadfile{urllib.parse.quote(fp, safe='/')}"
    part = f"{out_path}.part{threading.get_ident()}"
    last_err = ""
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            s = session_for(ip, auth)
            with s.get(url, stream=True, timeout=(10, 120)) as r:
                if r.status_code != 200:
                    last_err = f"HTTP {r.status_code}"
                    raise RuntimeError(last_err)
                with open(part, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        if on_chunk:  # GUI 实时进度用;CLI 不传,行为不变
                            on_chunk(len(chunk))
            size = os.path.getsize(part)
            with open(part, "rb") as f:
                magic = f.read(4)
            if magic != b"DHII":
                last_err = f"bad magic {magic!r} size={size}"
                raise RuntimeError(last_err)
            if expected > 0 and abs(size - expected) > 4096:
                last_err = f"size mismatch got={size} exp={expected}"
                raise RuntimeError(last_err)
            os.rename(part, out_path)
            return True, size, time.time() - t0, ""
        except (requests.RequestException, RuntimeError, OSError) as e:
            last_err = last_err or f"{type(e).__name__}: {e}"
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if attempt < retries:
                time.sleep(1 + attempt * 2)
                last_err = ""
    return False, 0, 0.0, last_err


def out_name(cam_name, seg):
    st = seg.get("StartTime", "").replace(":", "-").replace(" ", "_")
    et = seg.get("EndTime", "").replace(":", "-").replace(" ", "_")
    base = os.path.basename(seg["FilePath"])
    return f"{safe_name(cam_name)}_{st}_{et}_{base}"


def parse_channel_spec(spec):
    """'0-7,10,12' → set;'all' → None。"""
    if not spec or spec == "all":
        return None
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description="大华 NVR 批量 .dav 原始下载")
    ap.add_argument("--nvr", action="append", help="NVR IP,可多次;默认 4 台全上")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="", help="NVR 密码(必填,不设默认值)")
    ap.add_argument("--start", help='如 "2026-07-12 09:00:00"')
    ap.add_argument("--end", help='如 "2026-07-12 11:00:00"')
    ap.add_argument("--window", action="append", default=[],
                    help='时间窗 "start|end",可多次;与 --start/--end 二选一')
    ap.add_argument("--out", required=True, help="输出根目录(每摄像头一个子目录)")
    ap.add_argument("--channels", default="all", help='通道号,如 "0-7,10";默认 all')
    ap.add_argument("--select", default="",
                    help='每 NVR 指定通道,如 "10.10.50.1:4,5,7;10.10.50.2:0,1"(优先于 --nvr/--channels)')
    ap.add_argument("--name-filter", default="", help="按通道名称子串过滤,如 九教室")
    ap.add_argument("--per-nvr", type=int, default=4, help="每 NVR 并发数(默认 4)")
    ap.add_argument("--flat", action="store_true", help="不建子目录,全部平铺到 --out")
    ap.add_argument("--classroom-dirs", action="store_true",
                    help='子目录按教室名(通道名截到"教室"),而非完整摄像头名')
    ap.add_argument("--dry-run", action="store_true", help="只枚举统计,不下载")
    args = ap.parse_args()

    windows = [tuple(w.split("|", 1)) for w in args.window]
    if args.start and args.end:
        windows.append((args.start, args.end))
    if not windows:
        ap.error("需要 --start/--end 或至少一个 --window")
    if not args.password:
        ap.error("需要 --password")

    select = {}
    if args.select:
        for part in args.select.split(";"):
            ip, chs = part.split(":", 1)
            select[ip.strip()] = parse_channel_spec(chs)

    nvrs = list(select) if select else (args.nvr or DEFAULT_NVRS)
    auth = HTTPDigestAuth(args.user, args.password)
    ch_filter = parse_channel_spec(args.channels)
    os.makedirs(args.out, exist_ok=True)

    # === 1. 枚举:每 NVR 并行拉通道表 + 段列表 ===
    win_str = ", ".join(f"{s}~{e}" for s, e in windows)
    log(f"枚举 {len(nvrs)} 台 NVR,时间窗 {win_str}")
    jobs = []  # (ip, ch, cam_name, seg, out_path)
    seen_paths = set()
    skip_exist = 0

    def enum_nvr(ip):
        local = []
        try:
            titles = get_channel_titles(ip, auth)
        except requests.RequestException as e:
            log(f"!! {ip} 拉通道表失败: {e}")
            return local
        ip_filter = select.get(ip) if select else ch_filter
        for ch, name in sorted(titles.items()):
            if ip_filter is not None and ch not in ip_filter:
                continue
            if args.name_filter and args.name_filter not in name:
                continue
            all_segs = []
            for w_start, w_end in windows:
                segs = None
                for attempt in range(3):
                    try:
                        segs = find_segments(ip, auth, ch, w_start, w_end)
                        break
                    except (requests.RequestException, RuntimeError) as e:
                        if attempt == 2:
                            log(f"!! {ip} ch{ch} {name} 枚举失败: {e}")
                        else:
                            time.sleep(1)
                if segs:
                    all_segs.extend(segs)
            if all_segs:
                local.append((ch, name, all_segs))
        return local

    with ThreadPoolExecutor(max_workers=len(nvrs)) as ex:
        for ip, chans in zip(nvrs, ex.map(enum_nvr, nvrs)):
            n_seg = sum(len(s) for _, _, s in chans)
            total = sum(int(x.get("Length", 0) or 0) for _, _, s in chans for x in s)
            log(f"{ip}: {len(chans)} 通道 / {n_seg} 段 / {total / 1e9:.1f} GB")
            for ch, name, segs in chans:
                if args.flat:
                    cam_dir = args.out
                elif args.classroom_dirs and "教室" in name:
                    cam_dir = os.path.join(args.out, safe_name(name.split("教室")[0] + "教室"))
                else:
                    cam_dir = os.path.join(args.out, safe_name(name))
                for seg in segs:
                    op = os.path.join(cam_dir, out_name(name, seg))
                    if os.path.exists(op) or op in seen_paths:
                        skip_exist += 1
                        continue
                    seen_paths.add(op)
                    jobs.append((ip, ch, name, seg, op))

    total_bytes = sum(int(j[3].get("Length", 0) or 0) for j in jobs)
    log(f"待下载 {len(jobs)} 段 / {total_bytes / 1e9:.1f} GB(跳过已存在 {skip_exist} 段)")
    if args.dry_run or not jobs:
        return

    # === 2. 下载:全局线程池,每 NVR 信号量限流 ===
    sem = {ip: threading.Semaphore(args.per_nvr) for ip in nvrs}
    stats = {"ok": 0, "fail": 0, "bytes": 0}
    fails = []
    t_start = time.time()

    def run_job(job):
        ip, ch, name, seg, op = job
        os.makedirs(os.path.dirname(op), exist_ok=True)
        with sem[ip]:
            ok, size, dt, err = download_segment(ip, auth, seg, op)
        with _print_lock:
            if ok:
                stats["ok"] += 1
                stats["bytes"] += size
            else:
                stats["fail"] += 1
                fails.append((ip, ch, name, seg["FilePath"], err))
        done = stats["ok"] + stats["fail"]
        speed = stats["bytes"] / max(time.time() - t_start, 0.1) / 1e6
        tag = "OK  " if ok else "FAIL"
        log(
            f"{tag} [{done}/{len(jobs)}] {ip} ch{ch} {os.path.basename(op)} "
            f"{size / 1e6:.0f}MB {dt:.1f}s | 总速 {speed:.0f} MB/s"
            + (f" | {err}" if err else "")
        )

    workers = min(args.per_nvr * len(nvrs), 32)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run_job, jobs))

    elapsed = time.time() - t_start
    log(
        f"完成: OK={stats['ok']} FAIL={stats['fail']} "
        f"{stats['bytes'] / 1e9:.1f} GB / {elapsed:.0f}s "
        f"(均速 {stats['bytes'] / max(elapsed, 0.1) / 1e6:.0f} MB/s)"
    )
    if fails:
        log("失败清单:")
        for ip, ch, name, fp, err in fails:
            log(f"  {ip} ch{ch} {name} {fp} → {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
