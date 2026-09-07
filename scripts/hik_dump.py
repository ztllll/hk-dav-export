#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hik_dump.py — 海康 NVR 适配(ISAPI + HTTP Digest)

⚠️ 海康设备未经实测!本模块按海康官方 ISAPI 文档实现:
   《How to search and download the video file from NVR via ISAPI》
   (hikvisioneurope.com 技术合作伙伴文档)。流程:
   get_channel_titles(/ISAPI/ContentMgmt/InputProxy/channels)
   → find_segments(POST /ISAPI/ContentMgmt/search,CMSearchDescription,
     trackID = 通道号*100+1 主码流)
   → download_segment(GET /ISAPI/ContentMgmt/download?playbackURI=<搜索结果
     原样 URL 编码>)。
   文件为 MPEG-PS 流,保存为 .mp4(VLC / 海康 iVMS-4200 可播)。
对外函数签名与 nvr_dump.py 完全一致(通道参数为 0 基槽位;海康通道号 1 基,
内部已换算)。首次接入海康设备时,请先用一台设备小时间窗验证再批量使用。
"""
import os
import re
import time
import uuid
import threading
from urllib.parse import quote
from xml.sax.saxutils import unescape

import requests

_tls = threading.local()


def _session(ip, auth):
    key = "h_" + ip.replace(".", "_")
    s = getattr(_tls, key, None)
    if s is None:
        s = requests.Session()
        s.auth = auth
        setattr(_tls, key, s)
    return s


def is_hikvision(ip, auth):
    """/ISAPI/System/deviceInfo 可达且带 deviceName 字段 → 判定为海康"""
    try:
        r = _session(ip, auth).get("http://%s/ISAPI/System/deviceInfo" % ip, timeout=8)
        return r.status_code == 200 and "deviceName" in r.text
    except Exception:
        return False


def get_channel_titles(ip, auth):
    """通道表:{0基槽位: 名称}。优先 IP 通道(InputProxy),回退模拟通道(Video/inputs)。"""
    s = _session(ip, auth)
    titles = {}
    r = s.get("http://%s/ISAPI/ContentMgmt/InputProxy/channels" % ip, timeout=15)
    if r.status_code == 200 and "InputProxyChannel" in r.text:
        for blk in re.findall(r"<InputProxyChannel>(.*?)</InputProxyChannel>", r.text, re.S):
            mid = re.search(r"<id>(\d+)</id>", blk)
            mnm = re.search(r"<name>(.*?)</name>", blk)
            if mid:
                titles[int(mid.group(1)) - 1] = (mnm.group(1) if mnm else "").strip()
    if titles:
        return titles
    r = s.get("http://%s/ISAPI/Video/inputs/channels" % ip, timeout=15)
    if r.status_code == 200:
        for mid in re.findall(r"<id>(\d+)</id>", r.text):
            titles.setdefault(int(mid) - 1, "通道%s" % mid)
    return titles


def _fmt(t):
    """'2026-09-05 08:00:00' → '2026-09-05T08:00:00'(ISAPI 本地时间格式)"""
    d, hm = t.split(" ")
    return "%sT%s" % (d, hm)


def _parse_search(txt):
    """CMSearchResult → [seg dict];纯正则解析,零 XML 依赖,便于单测。"""
    segs, seq = [], 0
    for blk in re.findall(r"<searchMatchItem>(.*?)</searchMatchItem>", txt, re.S):
        muri = re.search(r"<playbackURI>(.*?)</playbackURI>", blk, re.S)
        if not muri:
            continue
        uri = unescape(muri.group(1).strip())
        ms = re.search(r"<startTime>([^<]+)</startTime>", blk)
        me = re.search(r"<endTime>([^<]+)</endTime>", blk)
        mlen = re.search(r"<contentLength>(\d+)</contentLength>", blk)
        start = (ms.group(1) if ms else "").replace("T", " ").rstrip("Z")
        end = (me.group(1) if me else "").replace("T", " ").rstrip("Z")
        seq += 1
        segs.append({"StartTime": start, "EndTime": end,
                     "Length": int(mlen.group(1)) if mlen else 0,
                     "Type": "hik",
                     "FilePath": "/hik/%s_%d.mp4" % (start.replace("-", "").replace(":", "").replace(" ", "_") or uuid.uuid4().hex[:12], seq),
                     "_playbackURI": uri})
    return segs


def find_segments(ip, auth, ch, start, end):
    """枚举一个槽位(0 基)时间窗内的录像段;trackID = 通道号*100+1(主码流)。"""
    s = _session(ip, auth)
    track = (ch + 1) * 100 + 1
    tpl = ("<?xml version='1.0' encoding='UTF-8'?>"
           "<CMSearchDescription>"
           "<searchID>%s</searchID>"
           "<trackList><trackID>%d</trackID></trackList>"
           "<timeSpanList><timeSpan>"
           "<startTime>%s</startTime><endTime>%s</endTime>"
           "</timeSpan></timeSpanList>"
           "<maxResults>250</maxResults>"
           "<searchResultPostion>%d</searchResultPostion>"
           "</CMSearchDescription>")
    segs, pos = [], 0
    for _round in range(40):                    # 250*40 段封顶,防死循环
        body = tpl % (str(uuid.uuid4()).upper(), track, _fmt(start), _fmt(end), pos)
        r = s.post("http://%s/ISAPI/ContentMgmt/search" % ip, data=body.encode("utf-8"), timeout=30)
        if r.status_code != 200:
            raise RuntimeError("search HTTP %s: %s" % (r.status_code, r.text[:120]))
        batch = _parse_search(r.text)
        segs.extend(batch)
        if not batch or len(batch) < 250:
            break
        pos += len(batch)
    return segs


def download_segment(ip, auth, seg, out_path, retries=2, on_chunk=None):
    """下载单段(POST 不通用,按文档用 GET download?playbackURI=),验 PS 头,返回 (ok, bytes, s, err)。"""
    uri = quote(seg["_playbackURI"], safe="")
    expected = int(seg.get("Length", 0) or 0)
    part = "%s.part%d" % (out_path, threading.get_ident())
    last_err = ""
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            with _session(ip, auth).get("http://%s/ISAPI/ContentMgmt/download?playbackURI=%s" % (ip, uri),
                                        stream=True, timeout=(10, 180)) as r:
                if r.status_code != 200:
                    last_err = "HTTP %s" % r.status_code
                    raise RuntimeError(last_err)
                with open(part, "wb") as f:
                    head = b""
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if len(head) < 4:
                            head = (head + chunk)[:4]
                            if len(head) == 4 and not (head[:3] == b"\x00\x00\x01" and head[3] in (0xBA, 0xBB)):
                                last_err = "bad PS magic %s" % head.hex()
                                raise RuntimeError(last_err)
                        if on_chunk:
                            on_chunk(len(chunk))
                        f.write(chunk)
            size = os.path.getsize(part)
            if expected > 0 and abs(size - expected) > 1 << 20:
                last_err = "size mismatch got=%d exp=%d" % (size, expected)
                raise RuntimeError(last_err)
            os.rename(part, out_path)
            return True, size, time.time() - t0, ""
        except (requests.RequestException, RuntimeError, OSError) as e:
            last_err = last_err or "%s: %s" % (type(e).__name__, e)
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
            if attempt < retries:
                time.sleep(1 + attempt * 2)
                last_err = ""
    return False, 0, 0.0, last_err
