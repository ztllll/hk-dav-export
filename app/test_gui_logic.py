#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 纯逻辑自检(无界面,assert 风格):python3 test_gui_logic.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nvr_gui as g

# ── parse_conn:每行 地址,账号,密码
conns, errs = g.parse_conn("10.10.50.1,admin,p1\n10.10.50.2,admin,p2")
assert len(conns) == 2 and not errs and conns[1]["pass"] == "p2"
conns, errs = g.parse_conn("10.10.50.1，admin，p1")            # 全角逗号
assert len(conns) == 1 and not errs
conns, errs = g.parse_conn(" 10.10.50.1 , admin , p1 ")       # 首尾空白
assert conns[0]["ip"] == "10.10.50.1" and conns[0]["pass"] == "p1"
conns, errs = g.parse_conn("10.10.50.1,admin\nbad-line,admin,p")
assert not conns and len(errs) == 2, (conns, errs)            # 缺字段 / 非法IP
conns, errs = g.parse_conn("10.10.50.1,admin,p\n10.10.50.1,x,y")
assert len(conns) == 1 and len(errs) == 1, (conns, errs)      # 合法后重复 → 报错
conns, errs = g.parse_conn("")
assert not conns and not errs

# ── norm_dt / parse_windows:标准格式 + 简写
assert g.norm_dt("2026-09-05 12:00:00") == "2026-09-05 12:00:00"
assert g.norm_dt("202609051200") == "2026-09-05 12:00:00"
assert g.norm_dt("20260905120030") == "2026-09-05 12:00:30"
assert g.norm_dt("") is None and g.norm_dt("xxx") is None
w, err = g.parse_windows([("2026-09-05 08:30:00", "2026-09-05 11:00:00")])
assert w == [("2026-09-05 08:30:00", "2026-09-05 11:00:00")] and not err
w, err = g.parse_windows([("202609050830", "202609051100")])
assert w == [("2026-09-05 08:30:00", "2026-09-05 11:00:00")] and not err
w, err = g.parse_windows([("", ""), ("2026-09-05 13:30:00", "2026-09-05 16:00:00")])
assert len(w) == 1 and not err
assert g.parse_windows([("2026-09-05 11:00:00", "2026-09-05 09:00:00")])[1]
assert g.parse_windows([("bad", "2026-09-05 09:00:00")])[1]
assert g.parse_windows([])[1]
assert g.parse_windows([("202613051200", "202614051300")])[1], "13月应报错"
assert g.parse_windows([("2026-09-05 08:00:00", "2026-09-05 10:00:00"),
                        ("2026-09-05 09:00:00", "2026-09-05 11:00:00")])[1], "重叠应报错"
w, err = g.parse_windows([("2026-09-05 08:00:00", "2026-09-05 10:00:00"),
                          ("2026-09-05 10:00:00", "2026-09-05 12:00:00")])
assert w and not err, "相接不重叠应通过"

# ── 目录命名 / 格式化 / 说明文本
assert g.cam_dir_for("/o", "九教室东", "flat") == "/o"
assert g.cam_dir_for("/o", "九教室东(前)", "classroom") == os.path.join("/o", "九教室")
assert g.cam_dir_for("/o", "九教室东(前)", "full") == os.path.join("/o", "九教室东(前)")
assert g.cam_dir_for("/o", "院门口", "classroom") == os.path.join("/o", "院门口")
assert g.fmt_ts(3723) == "1:02:03"
assert g.fmt_gb(163.5e9) == "163.5 GB"
assert g.fmt_gb(800e6) == "800.0 MB"
assert len(g.README_TEXT) > 200

print("GUI 逻辑自检通过")
