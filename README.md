# hk-dav-export

从大华(已实测)/海康(ISAPI,未经实测) NVR 批量导出**原始录像**(零转码)。Windows 单文件 GUI + 共用协议引擎 CLI。

许可证 [MIT](LICENSE)。**不要把 NVR 账号密码提交进仓库**;CLI 必须显式传 `--password`,GUI 连接框自行填写。

宏毅考试中心现场用法:大华 `mediaFileFind.cgi` + `RPC_Loadfile`,原始 `.dav`。

**必读顺序**:本文件(用法)→ `AGENTS.md`(项目约定/铁律)→ `docs/LESSONS.md`(协议细节/踩坑)。

## GUI 版(Windows 单体 exe)

成品 `app/dist/NVR_Export.exe`(~15 MB,双击即用,无需装 Python)。
功能:检测通道 → 勾选 → 填时间窗/目标目录 → 预检(段数/大小/估时/盘剩余)确认 → 实时进度导出
→ 自动在根目录写 `说明.txt`。断点续传与 CLI 同机制(重跑自动跳过已存在)。
源码 `app/nvr_gui.py`(tkinter,复用下方协议引擎);重新打包:`app/build_exe.bat`。

## 唯一脚本:`scripts/nvr_dump.py`

依赖:python3 + requests(系统已装)。

### 标准流程(三步)

```bash
cd /data/project/hk-dav-export

# 1. dry-run 看量(段数 + 总 GB),确认目标盘空间够
./scripts/nvr_dump.py --password "$NVR_PASS" --start "2026-07-12 08:30:00" --end "2026-07-12 11:00:00" \
  --out /tmp/x --dry-run

# 2. 正式下载(长任务务必 nohup 脱离前台)
nohup ./scripts/nvr_dump.py --password "$NVR_PASS" \
  --window "2026-07-12 08:30:00|2026-07-12 11:00:00" \
  --window "2026-07-12 13:30:00|2026-07-12 16:00:00" \
  --out "/run/media/hbhy/TOSHIBA_EXT/河北宏毅考试中心" \
  --classroom-dirs > /tmp/nvr_dump_run.log 2>&1 &

# 3. 完成后校验
find <out> -name '*.dav' | wc -l     # 应等于日志里的 OK 数
find <out> -name '*.part*' | wc -l   # 必须为 0
```

### 参数速查

| 参数 | 说明 |
|---|---|
| `--start/--end` | 单时间窗,`"YYYY-MM-DD HH:MM:SS"` |
| `--window "start\|end"` | 多时间窗,可重复(与 --start/--end 二选一) |
| `--out DIR` | 输出根目录(可直接指移动硬盘,不经本地盘) |
| `--nvr IP` | 限定 NVR,可重复;默认 4 台全上 |
| `--channels 0-7,10` | 通道号过滤(所有 NVR 生效) |
| `--select "ip:chs;ip:chs"` | **每 NVR 各选各的通道**(选路下载用这个),如 `"10.10.50.1:4,5;10.10.50.3:1,2"` |
| `--name-filter 九教室` | 按通道名子串过滤 |
| `--classroom-dirs` | 子目录按教室名(通道名截到"教室"),同教室多路合并一个目录 |
| `--flat` | 不建子目录全部平铺 |
| `--per-nvr N` | 每 NVR 并发(默认 4,**不要调大**) |
| `--dry-run` | 只枚举统计不下载 |

### 内建保障(不用额外处理)

- 断点续跑:重跑自动跳过已存在文件;`.part<tid>` 临时文件,验完 rename
- 每段验 `DHII` magic + 长度(±4KB),失败自动重试 2 次(带退避)
- 长连接复用(Digest 只握手一次);自动排除 Extra1 子码流
- 实时日志:每段 OK/FAIL + 全局速度;结束打总计和失败清单,有失败 exit 1

## NVR 信息

| IP | 覆盖 |
|---|---|
| 10.10.50.1 | 一~八教室(29 通道) |
| 10.10.50.2 | 九~十六教室(26 通道) |
| 10.10.50.3 | 十七~二十二教室(23 通道) |
| 10.10.50.4 | 公共区域:院/楼梯/走廊(20 通道) |

固件 `5.001 build 2026-03-31`,HTTP Digest 认证。账号密码请自行填写,不要提交到仓库。
通道名以实时 `ChannelTitle` 为准;`data/channel_map.tsv` 仅历史参考。

## 性能基准(2026-07-12 实测)

| 路径 | 吞吐 |
|---|---|
| NVR → 本机 SSD | ~50 MB/s(4 台聚合) |
| NVR → NTFS 移动硬盘 | ~25 MB/s |
| USB 移动硬盘对拷(rsync) | ~30 MB/s(独占时) |

163 GB / 490 段下载约 107 分钟,0 失败。

## 历史

2026-07-12 之前的 preflight/selection/download_v2 多脚本流程(慢、47% 失败率)已废弃删除,
根因:每段新起 curl 进程 + digest 重新握手 + 无重试。现单脚本替代。
