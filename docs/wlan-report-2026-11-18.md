# Skyward Recovery · 一加 13（dodge）WLAN 完整报告

- 版本：**v3 · 2026-11-18 03:40**（前一版 v2 为同日早间）
- 设备：OnePlus 13 / `OP5D0DL1` / `PJZ110` / ColorOS 17 / 内核 `6.6.118-android15`
- 底座：`PJZ110 17.0.0.103(CN01)`，OTA id `PJZ110_11.F.07_2070_202610080010`
- 槽位：`_b`；恢复分区 `/dev/block/by-name/recovery_b`（`/dev/block/sde62`，100 MiB）
- 编译：**手机自带 Debian 环境自编**（rustc 1.100.0 + NDK r29 + clang 19）
- 源码：`/workspace/skyward/dodgesrc`，HEAD `00b7ead`
- 镜像目录：`/sdcard/Download/一加13 Skyward rec移植/`

---

## 1. 结论摘要

| 项目 | 状态 |
|---|---|
| WLAN 驱动 / 固件 / 校准 | ✅ 通（校准 8.24～8.26 秒，PCIe/MHI/QMI 全走通） |
| 扫描 / 列表 | ✅ 通（一次扫描 46 个 BSS，列表正常显示） |
| WPA2 / WPA3 连接 | ✅ 通（四次握手成功，拿到 DHCP 租约） |
| 联网校验 | ✅ 通（`the network on wlan0 reaches the internet`） |
| 吞吐 | ✅ 实测 **7.4～10.3 MB/s**（468～864 Mbps 链路速率） |
| 恢复内下载 3.81 GB 大包 | ⚠️ 能下，但**每次到约 300 秒必复位**，最好一次下到 **1.84 GB（48%）**，`.part` 可续 |
| 移动数据（4G/5G） | ❌ 未做（Skyward 无 RIL；见 §10） |
| 稳定性（发布级） | ❌ **未达成**。这是目前唯一的 blocker |

**一句话**：WiFi 功能已经真正可用，但存在一个与库方案、与是否下载无关的「约 300 秒硬复位」。最新证据指向 **TEE（安全世界）在恢复里僵死 → RCU stall → panic**，而不是 WLAN 驱动或固件。

---

## 2. 已经实现的能力（全部实测）

### 2.1 WLAN 全链路

- 模块：`rfkill`（system_dlkm）→ `cnss2 / cnss_nl / mhi / pci-msm-drv …` → `cfg80211` → `qca_cld3_peach_v2`（vendor_dlkm）
- 设备树开关：`echo 1 > /sys/kernel/cnss/fs_ready`
- 接口：`wlan0` 在开机后 **37～43 秒**出现（`INTERFACE_WAIT` 60 秒）
- 固件：`WCNSS_qcom_cfg.ini` 放到 `/etc/firmware/wlan/qca_cld/peach_v2/`

### 2.2 厂商 `wpa_supplicant`（关键难点，已解决）

- 二进制：`/vendor/bin/hw/wpa_supplicant`，注册三个 AIDL 服务（`ISupplicant`、`IOplusSupplicant`、`ISupplicantVendor`），UID 1010
- **解决办法**：照上游 21 个机型的做法，`libraries = ["/system/lib64", "/vendor/lib64"]`，即**让 supplicant 跑在恢复自己的库上，厂商库只作后备**
  - 恢复的 `/system/lib64` 有 120 个库，`libbinder / libbinder_ndk / libbase / libutils / libcutils / liblog / libc++` 全在
  - 反过来的顺序（厂商在前）会加载 `/vendor/lib64/libbinder.so`，它依赖 `libapexsupport.so`，恢复里没有 → 一连锁的麻烦（见 §5）

### 2.3 恢复内下载（新功能）

```sh
# 方式一：命令行（镜像里带 /system/bin/dl 这个壳）
dl

# 方式二：直接给地址
/system/bin/recovery fetch https://… /sdcard/Download/xxx.zip

# 方式三：把地址写进文件，恢复里只敲 dl
/sdcard/Download/fetch.txt
  第一行 网址
  第二行 保存路径（可省，默认用网址里的文件名）
```

- 边下边落盘（每 16 MiB `sync_data`），中断后重跑自动从 `.part` 续
- 进度原地刷新：`2097152 of 4091845970 bytes (0.1%)`
- 拿到 DHCP 租约后自动写 `/etc/resolv.conf`，终端里不用手敲 DNS
- 实测：单连接 **7.4～10.3 MB/s**；3.81 GB 的包下到 **1.84 GB** 时被 300 秒复位打断

### 2.4 其它

- `/data` 解密：✅（weaver / keystore 通用化补丁）
- MTP 文件传输：✅（`init.rc` import + `skyward-mtp.rc`）
- 诊断设施：✅（见 §8）

---

## 3. 崩溃调查（本轮重点）

### 3.1 现象

| 版本 | 库方案 | 崩点（自开机） | 当时在做什么 |
|---|---|---|---|
| v85 | 厂商库在前 + 命名空间 | 299.76 s | 连上后空闲 |
| v93 | 同上 | 297.70 s | 连上后息屏静置 3 分钟 |
| v100 | 同上 | 302.27 s | **正在下载（523 MB）** |
| v103 | **上游方案：恢复自己的库在前** | **297.70 s** | **正在下载（1.84 GB）** |

**换了库方案、换了下不下东西，崩点一模一样。** 由此排除：库配置、下载负载、空闲省电。

另有一类更早的崩溃（48～50 秒，扫描收尾/发起连接时），属另一个触发点，见 §3.4。

### 3.2 判据一：它确实是内核崩溃

```
/proc/last_boot_reason  →  kernel crash Fulldump [2026-11-18 02:59:42]
```

时间戳与 v101 那次崩溃吻合。**所以不是 PMIC/看门狗硬复位，是 panic。**
此前 `pstore` 一直空，是因为**恢复镜像的 cmdline 里没有 `printk.always_kmsg_dump=1`**（boot image v4 头偏移 44 原本为空）。v102 起已把这条写进 cmdline。

### 3.3 判据二：TEE 先死，RCU stall 后炸

v103 的日志里，242 秒处：

```
oplus_bsp_dfr_hung_task_enhance: task adci_thread blocked for more than 120 seconds
  __scm_smc_call → qcom_scm_invoke_smc → si_object_invoke_ctx_invoke → adci_fn
oplus_bsp_dfr_hung_task_enhance: task cpudmof/6 blocked for more than 120 seconds
oplus_bsp_dfr_hung_task_enhance: task cpudmof/7 blocked for more than 120 seconds
```

- hung_task 阈值 120 秒、报在 242 秒 → **这些线程从约 122 秒起就卡死在 SCM（安全世界调用）**
- 而内核开关是：

```
/proc/cmdline → kernel.panic_on_rcu_stall=1
/proc/sys/kernel/panic_on_oops = 1
/proc/sys/kernel/panic_on_rcu_stall = 0（运行时，cmdline 里那条仍生效）
```

**推断链**：TEE 在约 120 秒失联 → 若干内核线程卡在 SCM → 某个路径持有 RCU 读锁/自旋锁 → RCU stall 检测（默认 21 秒）→ `panic_on_rcu_stall` → **panic → 复位**，叠加后正好落在 300 秒附近。

这条链能解释「与 WiFi 无关也崩」——WiFi 只是**让 TEE 更早被踩到**（supplicant 带 `libkeystore-wifi-hidl.so`，解密后 qseecomd/keymint/ssgtzd 仍在跑）。

### 3.4 另一类：48～50 秒的崩溃

发生在扫描收尾、发起连接的那几秒（`Auth TX` 都还没打出来）。触发点与 300 秒那类不同，目前只在部分启动出现（约一半）。两个触发点是否同一根因（TEE/RCU）尚未验证——**这正是 §9 第一个实验要分清的事**。

### 3.5 已知的环境缺陷（长期存在）

```
cnss: Failed to get cooling device node        （两次）
cnss_utils: WLAN MAC address is not set, type 0 → using default MAC address
cnss: DMS QMI connection not established       （刷屏后自行停止）
peach_v2: hdd_tsf_is_initialized: TSF is not initialized
peach_v2: hdd_config_power: power attr and opm attr is null
```

这些都记录在案，但**没有一条与崩点时刻吻合**，不能当结论。

---

## 4. 版本迭代（v70 → v103）

| 版本 | 镜像 SHA（前 8） | 关键改动 | 结果 |
|---|---|---|---|
| v70 | `9504f3c4` | 等待 20→60 秒；supplicant 日志进内核日志 | 接口 37 秒出；报 `libapexsupport` |
| v71 | `d31eae79` | 加 `qcom_dynamic_ramoops` | 加载成功，但 dump 仍无 |
| v72 | `9fbdc4f2` | 库路径加 `/mnt/system/lib64`（猜错一层） | 无变化 |
| v73 | `5874e6f7` | `LD_PRELOAD` 预装 libapexsupport | 换成 `openDeclaredPassthroughHal` 缺符号 |
| v74 | `156c49b4` | 内核日志每秒落 `/sdcard` | 写进未解密壳，重启即失 |
| v75 | `ed5a9455` | 落盘按 `st_dev` 重认；库走 4 个候选 | 落盘成功；定位真库在 `/mnt/system/system/lib64` |
| v76 | `9038fa7e` | 真系统库排第一 | `Mixing copies of libbinder` → 255 |
| v77 | `3bad0cf6` | 落盘滤噪；supplicant 日志整份复制 | 日志工程完成 |
| v78 | `76c9ddd8` | 库顺序厂商第一 | `liblog` 缺 `__hash_memory` |
| v79 | `0df20daf` | 私有目录只软链系统库 | `libbinder_ndk` 缺 `setTransactionCodeMap` |
| v80 | `bda6977c` | `LD_PRELOAD` 真 libbinder | 链接全通，仍两份 libbinder → 255 |
| v81 | `17c0556f` | **自写 `/linkerconfig/ld.config.txt`（命名空间）** | 起来、注册服务、扫到 46 BSS、出列表；随后崩 |
| v82 | `6d931f0e` | 落盘 300 ms、窗口 1500 行 | **连接成功：864 Mbps、IP、网关** |
| v83 | `4299da28` | 命名空间配置 gate + 起完即撤 | 连上，跑 2 分钟无碍 |
| v84 | `cc7d2d18` | 再滤一层噪声 | 日志更干净 |
| v85 | `099892eb` | `/metadata` 落盘 + 心跳 + 断档记录 | 连上；299.76 s 崩 |
| v86 | `4aeb2534` | 关 WoW/SAP 挂起 + 挂 pstore | 连上就崩（后证与它无关） |
| v87 | `08dfce4c` | 未解密时挂 `/metadata` | 目录项没落盘，文件消失 |
| v88 | `2b9d3094` | 目录项 fsync | 依旧无文件（未解密） |
| v89 | `4f95c4bb` | `/metadata` 只读则卸了重挂 | **成功**：`/metadata is mounted writable` |
| v90 | `8710619b` | 撤回 WoW 改动 | 仍崩 |
| v91 | `6c510945` | 回到 v83 的 WLAN 路径（撤脚手架） | 仍崩（49.38 s）→ 排除脚手架 |
| v92 | `5ce7c80d` | 修 `[schedu]` 过滤误伤；落盘回 1 秒 | 列表恢复可见 |
| v93 | `008acbf9` | `panic_on_oops=1` + `panic=20` | 仍无 dump（因为缺 always_kmsg_dump） |
| v94 | `93d5b8f0` | 每 20 秒保活 | 见 v100 |
| v95 | `bbf05150` | `fetch` 子命令 + 写 resolv.conf | 下载可用 |
| v96 | `822aecf8` | `fetch.txt` + `/system/bin/dl` | 恢复里敲 `dl` 即可 |
| v97 | `18b689a5` | 进度原地刷新 | 终端单行刷新 |
| v98 | `7a687609` | 落盘回到 `/metadata` + `/sdcard` | 不解密也有日志 |
| v99 | `4b80894e` | 预设网络（**已撤**，见 §5） | — |
| v100 | `3b188c19` | 前 60 秒 200 ms 落盘 | 下载 523 MB 后 302 s 崩 |
| v101 | `9ba49dbe` | 起 `cnss-daemon` | DMS 刷屏后仍停；48.6 s 崩 |
| v102 | `69cfb013` | **cmdline 注入 `printk.always_kmsg_dump=1`** | 取证手段就位 |
| **v103** | `d84070fc` | **按上游：恢复自己的库在前，不写命名空间** | 下载 **1.84 GB**；297.7 s 崩 |

现存镜像：`recovery.img`（原厂）、`skyward-dodge-our.img`（无 WLAN 日常版）、`v42-baseline`、`v83-final`、`v91-lean`、`v92-scan`、`v93-panic`、`v94-keepalive`、`v95-fetch`、`v96-dl`、`v97-dl2`、`v98-dl3`、`v101-cnssd`、`v102-kdump`、`v103-upstream`。

---

## 5. 已排除的判断（不要再引用）

| 曾经的判断 | 被什么推翻 |
|---|---|
| 20 秒没接口 = 驱动坏 | 接口 37～43 秒才出；60 秒预算正常 |
| `cfg80211: failed to load regulatory.db` 是病因 | 该文件本就不存在，原厂同样报 |
| `cnss_softsku_peach.pfm` 缺 / `TME patch err 83` | 零售机常态，固件照常 |
| `Unbalanced IRQ 366 wake disable` 是主因 | 冷启动校准关机路径的 WARN |
| 崩溃不是 panic，是 PMIC/看门狗 | **错**：`/proc/last_boot_reason` = `kernel crash Fulldump` |
| `pstore` 空 = 没有 panic | **错**：是 cmdline 缺 `printk.always_kmsg_dump=1` |
| 命名空间是唯一解法 | **不成立**：上游 21 机型在单命名空间下跑得好，库顺序对即可 |
| `libraries` 用厂商在前 | **错**：会拉进厂商 libbinder → 依赖 `libapexsupport` → 整条链崩 |
| 关 WoW/SAP 挂起导致崩溃 | 撤回后照样崩 |
| 调试脚手架（挂 /metadata、spawn sh、改 ini）导致崩溃 | v91 全撤后照样崩 |
| 预设网络能绕开 AIDL 连接路径 | **错**：Skyward 走控制套接字（`ADD_NETWORK`/`SET_NETWORK`/`ENABLE_NETWORK`），本来就不经 AIDL |

---

## 6. 手机侧工具链（可复现）

```sh
/opt/rust/bin/{rustc,cargo}                    1.100.0（host aarch64-unknown-linux-gnu）
/opt/rust/lib/rustlib/aarch64-linux-android/   已装
/workspace/toolchain/ndk/android-ndk-r29/…/sysroot
Debian clang 19.1.7 + ld.lld-19
/workspace/cargohome                           TUNA 源（sparse）

# 两个必须知道的坑
1) NDK r29 的 sysroot 不再带 libunwind.a：从 ndk.zip 抽
   toolchains/llvm/prebuilt/linux-x86_64/lib/clang/21/lib/linux/aarch64/libunwind.a
   → /workspace/toolchain/lib/aarch64/，链接加 -L
2) host rust-std 必须落在 lib/rustlib/aarch64-unknown-linux-gnu/lib

sh /workspace/skyward/build_universal.sh                          # 约 10 秒增量
python3 /workspace/skyward/pack_v102.py <base> <elf> <toml> <out> # 换 ELF/TOML + 注入 cmdline
python3 /workspace/skyward/verify68.py <old> <new>                # 逐文件核对
```

`pack_v102.py` 额外做两件事：把 `printk.always_kmsg_dump=1` 写进 boot image v4 头的 cmdline（偏移 44），以及往 ramdisk 里加 `/system/bin/dl`（`recovery fetch` 的壳）。

---

## 7. 恢复模式挂载布局（实测）

```
/mnt/system         ← 系统根（其下还有 system/ data/ apex/ …）
/mnt/system/system  ← 真正的 /system（lib64、etc、framework…）
/mnt/system/lib64   ← 不存在
/vendor             ← vendor_b（erofs）
/odm /mnt/product /mnt/system_ext /vendor_dlkm /system_dlkm …
/system             ← ramdisk 自己那层（120 个库）
/metadata           ← f2fs 62 MB（明文）
/data               ← 解密后 f2fs
```

`/system/lib64` 里的 `libc.so / libm.so / libdl.so` 在真系统是指向 APEX 的软链，恢复里 APEX 未挂载 → 悬空 → 自动回退 ramdisk 那层。

---

## 8. 诊断设施（发布前建议保留）

| 设施 | 行为 |
|---|---|
| `/metadata/skyward/wlan-diag/tail.log` + `/sdcard/Skyward/wlan-diag/tail.log` | 内核日志尾部两份；**前 60 秒每 200 ms、之后 1 秒**；先写 `tail.tmp` → fsync → 改名；目录项也 fsync |
| 噪声过滤 | `OPLUS_CHG`、`audit: type=1400`、`si-mo:`、`tri_state_key`、`syna_`、`[wdog_util]`、`iostack:`、`CMN_MLME`、`ROAM…`、`healthd:`、`black_screen_check`、`bright_screen_check`、`[OPLUS_WD]`、`wcd_usbss` |
| `/metadata` 挂载 | 先试建目录；只读则 umount 再挂可写；成败写日志 |
| `/sdcard` 重认 | 按 `stat("/sdcard").st_dev` 变化重新认（解密前后是两套文件系统） |
| 心跳 | 每 20 秒 `wlan: mirror beat, N folders hold the log` |
| supplicant 原话 | 整份复制到 `/sdcard/Skyward/wlan-diag/wpa_supplicant.log` |
| 起 supplicant 前自检 | `LD_LIBRARY_PATH`、七个关键库的落点，逐条写内核日志 |

---

## 9. 下一步（按信息量排序）

### 实验 0（**不用刷机**，今晚就能做）

**进恢复，什么都不做，不点 WiFi，静置 8 分钟。**

- 若照样约 300 秒复位 → 与 WLAN 无关，是恢复环境的 TEE/RCU 问题；
- 若 8 分钟不崩 → 才轮到 WLAN。

### 若实验结果指向 TEE/RCU

1. **解密完成后停掉** `qseecomd` / `keymint` / `ssgtzd`，让安全世界安静下来（改动最小、最贴合证据）；
2. 把 `kernel.panic_on_rcu_stall` 置 0，让它“卡而不重启”，日志里留下 `rcu detected stalls` 完整栈；
3. 再不行才考虑不启动 keymaster 那套（代价：不能解密 → 下载只能落 U 盘 `/mnt/usb`）。

> 不解密时的落点提醒：`/metadata` 只有 **62 MB**（剩 18.5 MB），`/tmp` 是内存盘（可用约 7.2 GB，占满即 OOM），**唯一靠谱是 U 盘**（Skyward 挂到 `/mnt/usb`）。

### 若实验结果指向 WLAN

再回到驱动侧，按此顺序：`gIPAConfig` / IPA perf 路径 → MLO/11ax 特性 → 发谢功率相关 ini 键。

### 下载侧的兜底（与上面并行）

服务器支持 `Accept-Ranges: bytes`，`.part` 可续。若短期修不好，可做**多段并发下载**：8 路 Range 并行，实测单路 7.4～10 MB/s，8 路有望 1～2 分钟内下完 3.81 GB，从而躲开 300 秒窗口。这只是权宜，不是修复。

### 移动数据（4G/5G）

- 现状：Skyward 无 RIL 支持。需要 `qcrilNrd`（`/vendor/bin/hw/` 有，`init.qcrild.sh` 也有）+ QMI + SIM + 数据拨号 + APN + rmnet + IP 配置；
- 工作量比 WLAN 大一个数量级，且同样要过射频；
- 建议：**先把 WiFi 这条时间炸弹定性并解决**，再评估移动数据。

---

## 10. 代码提交（`/workspace/skyward/dodgesrc`）

```
00b7ead  wlan: 加下载入口（fetch/dl）、保活、cnss-daemon，并按上游把库顺序改回去
f14c12b  wlan: 打开 panic_on_oops，给硬复位留一线可能
2e7e691  wlan: 别把扫描/连接的行一起滤掉；落盘回到 1 秒
224c073  wlan: 撤回关 WoW/SAP 挂起的改动
ffd6c32  wlan: /metadata 若已只读挂载，先卸再挂成可写
65f1c8d  wlan: 落盘连目录项一起 fsync，挂 /metadata 的成败也写进日志
22c33d5  wlan: 未解密时把 /metadata 挂成可写，日志不再无处可落
998a8eb  wlan: 恢复里关掉 WoW 与 SAP 挂起，并把 pstore 挂起来
23584ee  wlan: 落盘再加 /metadata 一份，并记下休眠造成的断档
f8a1afe  wlan: 落盘再滤一层噪声（漫游扫描样表、心跳、屏幕检查）
d6b951e  wlan: 命名空间配置只在本机布局下写，起完 supplicant 即撤
a4b8d16  wlan: 一加13 在恢复里用命名空间跑通厂商 supplicant
ba989f0  crypto/programs: 解密链路通用化
4183345  wlan: supplicant 输出落盘，便于诊断退出码 1
```

**全部只在本地，未推送。** 发布前需要决定：命名空间那几笔是否回退（上游方案已证明够用），以及诊断设施是否随包发布。

---

## 11. 发布前必须回答的问题

1. 300 秒复位到底是不是 TEE/RCU？（实验 0 就能定性）
2. 48～50 秒那一类崩溃与它是同一根因吗？
3. 若无法消除，是否接受「把 WiFi 标为实验性、仅在需要时开启、并提示可能复位」？
4. 移动数据是否列入路线图（工作量评估见 §9）？

**在这些问题有答案之前，不建议对外发布带 WLAN 的版本。** 功能已经能用，但一个会随机复位的恢复环境，对别人的手机是不负责任的。
