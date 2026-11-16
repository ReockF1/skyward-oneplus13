# Skyward Recovery → 一加 13（dodge）移植总报告

**整理日期**：2026-11-16
**整理范围**：本机全部镜像产物、三份既有报告、以及 2026-11-16 全天的实操会话记录（v2 → v34）
**性质**：阶段性汇总。凡未实测的推断均已单独标注，不与实测结论混排。

---

## 0. 三句话摘要

1. 显示本身**已经不是主要障碍**：v2 在 0.3.1 上打了亮度补丁后实机出过画面，触摸正常；v32 起自编 main 版本里面板也确实上电（`oplus_panel_power_on`、`dsi_display_set_mode … fps=120`）。
2. 之后反复的“黑屏”**是三种不同故障共用一个外表**：亮度断言硬失败（已修）、服务启动后静默停住（未解）、显示提交链失败或语义错误导致进程退出（正在追）。
3. 真正的瓶颈不是显示代码，而是**启动顺序**：Skyward 先解析配置、加载模块、挂分区、起厂商服务，最后才上屏；任何一环出错都表现为“没界面”，且 init 每 5 秒重拉一次，形成看似无限复发的黑屏。

---

## 1. 设备与镜像基线

| 项目 | 值 |
|---|---|
| 机型 | OnePlus 13 中国版，`PJZ110`，代号 `dodge` |
| 系统 | ColorOS 17 / Android 17，内核 `6.6.118` |
| 显示驱动 | `/vendor_dlkm/lib/modules/msm_drm.ko` |
| 面板 | `qcom,mdss_dsi_panel_AA569_P_3_A0019_dsc_cmd`（Novatek NT37801 AMOLED，cmd，DSC 必需） |
| 面板 DT 路径 | `/proc/device-tree/soc/qcom,mdss_mdp@ae00000/<面板名>`（第二层，`soc` 下无直接子节点） |
| DSI 模式 | `1440x3168x120cmd` / `125cmd` / `130cmd` / `138cmd`（全 cmd） |
| 背光节点 | `/sys/class/backlight/panel0-backlight`，max 4094 |
| 触摸 | `event8` `touchpanel`，`INPUT_PROP_DIRECT`，ABS `0..23040` / `0..50688`（原始值 ÷16 即像素） |
| 马达 | `lra_frequency_hz=131`、`t_lra_us=7692`，`input4 qcom-hv-haptics` |
| recovery 分区 | `recovery_a` / `recovery_b` 各 204,800 扇区（100 MiB），当前槽 `_b` |
| 镜像封装 | Android boot header **v4**，`kernel_size=0`，ramdisk 为 LZ4 legacy，AVB footer 按新尺寸重算 |

### 目录里两个容易踩的坑

- `skyward-pd2301-0.3.1.img`（201,326,592 B）**不是一加 13 的包**，是 vivo iQOO Neo8（PD2301 / SM8475）的，其 `device.toml` 写着 `codename="PD2301"`、`brand="vivo"`。
- 因此“目录名 = 内容”这个假设不成立，取 ELF / toml 前必须先核对 `codename`。

---

## 2. 版本谱系（每版都实机刷过）

| 版本 | 构成 | 结果 |
|---|---|---|
| v1 | 上游原始 dodge 构建 | 黑屏，`recovery failed: panel … gives its normal maximum as none of […])`，19/24/29/34s 反复 |
| **v2** | 0.3.1 + 亮度补丁，配置不动 | **出画面、触摸正常**，data 打不开 |
| v3 | v2 + 七个 TEE 服务（qseecomd/keymint/gatekeeper/weaver/keystore2/strongbox） | 仍有画面，data 仍打不开 |
| v4 | v3 + 日志落盘看门狗 | 取证用，机制可用 |
| v5 | v2 的 ELF + 别人完整 `device.toml` + 去掉三行版本属性 | **卡住无界面** |
| v6 | v5 去掉三个 NFC 服务的 `ready_service` | 卡住无界面 |
| v7 | v5 再去掉全部 `[[service]]` / `[[owners]]` | 卡住无界面 |
| v15–v27 | 中间实验（镜像名分别标注 tee2 / normalmax / waitsimple 等；本报告未逐一复核内部差异） | 均为黑屏或不可用 |
| v28 | 别人能用的 ramdisk + 主人自编 main ELF（含 `//` 注释修复），仅换 ELF | 解析改善，仍需继续推进 |
| v29 | main + 诊断型看门狗 | 取证 |
| v30 | main + 去掉 NFC 服务 | 黑屏 |
| v31 | main + dmesg 采集 | 取证 |
| v32 | main + `//` 与 `/* */` 两处注释解析补丁 | **首次出现“199 个服务被读入”**；面板上电，仍黑屏 |
| v33 | v32 + 0.3 秒密集采样 | 只采到 **2 个样本（0.6 秒）**即中断 |
| v34 | v33 去掉 `oneshot`，看门狗常驻并置于服务列表末尾 | 已生成，**尚未刷写、暂无日志** |

产物路径：`/sdcard/Download/一加13 Skyward rec移植/`，各版均为 104,857,600 B。

---

## 3. 三类黑屏，分开看

### 3.1 第一类：亮度上限断言硬失败（已闭合）

`crates/skyward/src/ui.rs` 的 `normal_max()`：

```rust
let (normal, full) = NORMAL_MAX
    .iter()
    .find(|(normal, _)| node.join(normal).exists())   // 只判断每组第一个属性
```

`NORMAL_MAX` 每组是 `(normal, 回退)`，`find` 只看前者；而本机面板只有 `oplus,dsi-bl-normal-max-level` 与 `qcom,mdss-dsi-bl-max-level`，`mi,` 三组全落空 → 直接 `recovery failed` → 进程退出 → init 重拉 → 黑屏。

修法：新增 `("oplus,dsi-bl-normal-max-level", "qcom,mdss-dsi-bl-max-level")` 一组，并改成“先找 normal，找不到退化成 normal = full”的两轮查找。**修完画面与触摸即正常。**

> 待核实：`oplus,dsi-bl-normal-max-level` 的实测值在两份旧文档里分别记为 3515 与 4094，需要重新 `od` 读一次 DT 节点定案。

### 3.2 第二类：起服务后静默停住（未解）

v5–v7 的缓存日志结尾分别是：

```
[ 6.85] the service directory is told of 4 of the phone's own services
[ 9.49] ran /system/bin/sh -c echo start > /sys/class/remoteproc/remoteproc1/state; sleep 2
[ 9.72] /odm/bin/hw/android.hardware.nfc-service.nxp: pid 797 ended: signal: 6 (SIGABRT)
[10.74] … 每 1 秒重试，持续到 39 秒以上
```

```
[38.03] 1 of the phone's own files handed to the accounts its init gives them
[38.03] the service directory is told of 1 of the phone's own services
（到此为止，无报错、无后续、无界面）
```

即：**没有 panic、没有错误行，就是停在那里不再前进**。v5 还叠加了 `nfc-service.nxp` 每秒一次 `SIGABRT` 重启循环。

对比 v32（main 版本）：同样位置变成 `told of 199 of the phone's own services`，说明**这一关在 main 分支上已经能过**，0.3.1 的 `[[service]]` 实现是卡点之一。

### 3.3 第三类：显示提交链 / 语义错误（正在追）

v32 的尾部：

```
[103.10] the service directory is told of 199 of the phone's own services
[105.11] ran /system/bin/sh -c echo start > /sys/class/remoteproc/remoteproc1/state; sleep 2
[105.11] devices not brought up: make out from the phone's partitions how its keys are used:
         /odm/bin/hw/android.hardware.weaver-service.nxp is a weaver of a make the recovery does not know
```

同时 dmesg 里已有 `oplus_panel_power_on`、`dsi_display_set_mode … fps=120`，但 `ps` 里**没有 recovery 进程**，屏幕黑。

由此可得两条并列可能：

1. weaver 的 `make` 不被识别，key 链中止，后续流程返回错误 → 进程退出（日志显示该行本身是“非致命”打印，所以还需确认错误是否向上传播）；
2. 显示侧进入 `atomic commit` / 等待 page-flip 后失败（对应文案 `atomic commit`、`the display did not complete the commit within {n} ms`、`read display events`、`wait for the display`）。

显示链静态证据（dodge 构建内地址）：

`0x39f938 → 0x3f6964（标志 0x601） → 0x4009b4 → 0x40add8（ioctl 0xC03864BC = DRM_IOCTL_MODE_ATOMIC） → 0x3f8260（2000ms 等待 event）`

`0x601 = PAGE_FLIP_EVENT | NONBLOCK | ALLOW_MODESET`，未设 `TEST_ONLY`，是真实提交。四组数组（objs / count_props / props / prop_values）的**上游生产者仍未还原**，对象→属性→值对应表未建立。

---

## 4. 已闭合并可复用的结论

| 结论 | 证据 |
|---|---|
| 亮度查找的成对探测 bug + 只认 `mi,` 属性是本机第一道死因 | 源码 + pstore 日志 + v2 实机 |
| 面板名带 `:PanelID-…` 后缀，DT 节点在第二层 | `dsi_display0` 原文、`/proc/device-tree` 实测 |
| 0.3.1 的 `[[service]]` 在本机起不来（NFC 服务 SIGABRT 循环，v7 全删后静默停住） | v5–v7 缓存日志 |
| `//` 与 `/* */` 注释会让脚本解析在 `begins no section` 处失败；两处补丁后 199 个服务可读入 | v32 缓存日志 |
| `[[service]]` 服务若 `oneshot = true`，主进程 `exit 0` 后整组被杀，后台采样只活 0.6 秒 | v33 仅 2 个样本 |
| 系统会在回系统后清 pstore，但 cache 分区尾部（255 MiB 处，块 65280）写入的日志能保留 | 多次 dd 读回成功 |
| 两套 ELF 的 toml 字段不兼容：0.3.1 有 `host_mode`/`authors`/`shown_model`，dodge 构建无；0.3.7 有 `mtp`/`period`/`unblock` | 字符串表比对 + 报告 |
| 写错字段 = toml 解析失败 = 立即黑屏且无日志 | 静态结论 + v5 复现 |

---

## 5. 未决问题（按优先级）

1. **v28–v34 的黑屏到底停在哪一句**：需要 v34 的常驻看门狗把 `recovery failed:` 那一行抓到。这是当前唯一能一次定性的动作。
2. **weaver `make` 不被识别**：需要 main 源码里该错误字符串的上下文（已知名单从哪来、能否白名单或跳过），再决定补丁。
3. **显示提交是否真的失败**：若日志出现 `atomic commit` 或 `did not complete the commit`，则要把 0.3.7 的亮度/模式处理方式搬过来（它的 ELF 里没有 `oplus,` 属性字符串，说明它用了别的办法）。
4. **Wi‑Fi 为什么在 0.3.7 包上仍不通**：候选顺序为 modem/remoteproc 时序 → `rfkill` 来自 system_dlkm 的加载顺序 → `qca_cld3_peach_v2` 依赖缺项 → `fs_ready` 写过早。
5. **`density` 不符**：现配置 560，实机 `ro.sf.lcd_density=640`，参与 UI 布局，且存在硬失败路径，建议下一版一并改。

---

## 6. 两条建议路线

### 路线 A：回 v2 形态，最小配置逐步加（推荐先做验证）

- 以 v2 为基线（能出画面），**一次只加一个变量**；
- 第一刀只加 weaver 的 make 白名单，不动 NFC、不动 wlan；
- 每版都必须抓到缓存日志末尾，否则视为无效实验。

优点：每一步都有“画面是否回来”这个明确信号；缺点：0.3.1 的 `[[service]]` 缺陷仍在，可能很快再撞墙。

### 路线 B：直接锁 0.3.7 线

- 显示、触摸、data 解密、MTP、分区挂载已全部可用，只剩 WiFi；
- 用 main 分支自编 ELF 替换 0.3.7 的 ELF 容易引入新变量（v28 起就在这条线上折腾）。

优点：功能覆盖最全；缺点：WiFi 与 weaver 仍需单独解，且自编 ELF 会改变二进制基线，回归面变大。

**取舍建议**：先用 v34 拿到那一行死亡日志，再决定走 A 还是 B——现在换路线等于放弃唯一的定性机会。

---

## 7. 只读取证命令清单（不含写入）

```bash
# 1) 读 cache 分区尾部的日志块（只读，不写）
dd if=/dev/block/by-name/oplusreserve2 bs=4096 skip=65280 count=48 2>/dev/null > /data/local/tmp/l34.bin

# 2) 段数与标记
strings -n 4 /data/local/tmp/l34.bin | grep -a -c "SKYLOG34"
strings -n 4 /data/local/tmp/l34.bin | grep -a -c -- "--- t="

# 3) 死亡行
strings -n 8 /data/local/tmp/l34.bin | grep -a "recovery failed" | tail -8

# 4) 显示相关
strings -n 8 /data/local/tmp/l34.bin | grep -aE "atomic commit|did not complete the commit|read display events|wait for the display"
```

刷写前必备份原厂 recovery，并确认当前槽位（`_b`），刷后等待 30 秒再回系统。

---

## 8. 待核实项（避免把旧结论当定论）

- `oplus,dsi-bl-normal-max-level` 实测值是 3515 还是 4094。
- v15 / v27 / v29 / v30 / v31 的内部差异（本轮只按镜像名与产物哈希归档，未逐版解包复核）。
- v28 起自编 main ELF 的 `sha256` 与源码 commit 对应关系（`d200127b…` ↔ commit `c5e09f6` main）。
- v34 从未实机刷写，日志为空，**不能**据此判断任何显示结论。

---

## 9. 0.3.7 静态拆解追加（夜间补）

对象：`/workspace/skyward/root_theirs/system/bin/recovery`（8,804,608 B，`956dc263…`）对照 `new_recovery`（0.3.1，`38184993…`）与 `root_bootfix`（dodge 官方，`cf3869fc…`）。

### 9.1 亮度：0.3.7 是把断言删了，不是换了算法

| 字符串 | 0.3.7 | 0.3.1 |
|---|---|---|
| `panel …: normal maximum … out of …`（纯打印） | 1 处 | 1 处 |
| `… gives its normal maximum as none of …`（致命 Err） | **0 处** | 1 处 |
| `oplus,dsi-bl-normal-max-level` | 0 处 | 1 处（我们的补丁） |
| 读 `/sys/class/backlight` + `max_brightness` | 有 | 有 |

结论：0.3.7 不靠查 DT 属性算上限，而是**不再把查不到当致命错误**。v2 的补丁方向是“把检查修对”，0.3.7 的方向是“不要这个检查”。

### 9.2 脚本解析器：0.3.7 认注释，0.3.1 不认

| 字符串 | 0.3.7 | 0.3.1 |
|---|---|---|
| `: a comment is not closed` | 1 处 | **0 处** |
| `//importservice…`（字面量以 `//` 开头） | 有 | 无（该串以 `import` 开头） |
| `begins no section` | 1 处 | 1 处 |
| `… is not there after …` | 有 | 0 处 |
| `wait for a file the modules just loaded make` + `_a`/`_b` | 有 | **0 处** |

解释力：v5–v7 日志里的 `told of 4` / `told of 1`，正是 0.3.1 解析器在第一个 `//` 注释处早停的结果；main 分支同样问题，v32 打完 `//` 与 `/* */` 两个补丁后立刻变成 `told of 199`。**我们那两刀，等于手工补回了 0.3.7 早已具备的能力。**

### 9.3 spawn 原语完全一致

三颗 ELF 的动态导入表都是 210 项，`fork`/`execvp`/`setgroups`/`setuid`/`setgid`/`setsid`/`pipe2`/`waitpid` 全都有且同名同序。**差异不在“怎么 fork/exec”，而在“什么时候起、起之前等什么”。**

### 9.4 由此得到的因果链（推断，等日志验证）

1. 0.3.1 解析 init.rc 早停 → 只认到 4 个服务 → 直接按 `device.toml` 里的少量服务硬起；
2. 0.3.7 解析完整（199 个）+ 起服务前有“等模块文件出现”（带 `_a`/`_b` 槽位）的闸门 → NFC/weaver 等依赖项就绪后再起，所以不崩；
3. 0.3.1 没有闸门，NFC 抢跑 → `/dev/nq-nci` 未就绪 → 每秒 `SIGABRT`；把服务全删（v7）后则停在“told of 1”之后不再前进。

待验证点：v34 的常驻看门狗日志是否显示早停（`told of N` 偏小）与后续等待位置。

### 9.5 0.3.7 相对我们的“特别之处”（字符串差分，已过滤编译路径噪声）

| 维度 | 0.3.7 | 我们（0.3.1 打补丁） | 影响 |
|---|---|---|---|
| 致命错误策略 | 等不到/对不上就降级继续（`; going on without it`、`… is not there after …`、`… is not there to write …`） | 直接 `Err` 返回（亮度断言、解析早停） | 它是“尽量活着”，我们是“一处不对就退出” |
| init.rc 解析 | 认 `//` 与 `/* */`，能报 `: a comment is not closed` | 遇到 `//` 早停，只认到 4 个服务 | 直接决定服务能否起 |
| 就绪闸门 | `wait for a file the modules just loaded make` + `_a`/`_b` 槽位 | 无 | NFC/weaver 抢跑 → SIGABRT |
| OTA / 更新 | `a backup's folder has a name`、`the update's merge goes on`、`the cache partition was found as the parts were`、`read it from the archive` / `put it in place` | 无 | 它能处理 OTA 包与备份，我们不能 |
| 配置字段 | 多 `backup` `update` `stock` `unblock` | 多 `host_mode` `authors` `shown_model`、`probe` | 字段互不兼容，写错即解析失败黑屏 |
| 模块缺失 | `4 is listed in a modules.dep but missing from all of …`（提示后继续） | 无对应容错 | 少个模块不再等于死 |
| 属性/清单 | 读 `ro.boot.product.vendor.sku`、`ro.boot.product.hardware.sku`、`etc/vintf/manifest.xml` | 无 | 机型/SKU 判断更细 |
| 马达 | `[haptics] period: `、`says the motor's period is 0 us` | 无校验 | 震动参数出错会提示而非静默 |

一句话：**0.3.7 不是一个“更适合 dodge 的包”，而是一条更新的分支——它把“致命检查”换成了“尽量继续 + 就绪等待 + 完整解析”。** 我们在 0.3.1 上加的每个补丁，本质上都是在追这条分支已有的能力。

## 10. 0.3.7 深挖：四项全部落地（2026-11-16 夜）

### 10.1 `WEAVERS` 表 —— 已核实，我们的补丁与它逐项一致

| 项 | 0.3.7 | main（打补丁前） |
|---|---|---|
| `.thales` | 1 | 1 |
| `.nxp-qti` | 1 | 1 |
| **`.nxp`（独立字面量，后接 0 字节）** | **1** | **0** |
| `weaver-service` | 1 | 1 |
| `vendor.goodix…IGdxEse` | 1 | 1 |

结论：0.3.7 的 `WEAVERS` 比 main 多一项 `(".nxp", Through::ElementService)`。我们在源码里补的那一刀与它一字不差，`ends_with` 也不会误吃 `.nxp-qti`。

### 10.2 就绪闸门

字符串 `wait for a file the modules just loaded make` 位于 `0x36521`，其字面量块紧邻 `A`、`B`、`_a`、`_b`——即按 **A/B 槽位**拼路径再等待。代码引用点：`0x30550c`（`adrp x22,#0x36000; add x22,x22,#0x521`），所在函数按 `0x50` 字节步进遍历一个条目数组，逐条调用后再格式化这条消息。main 里该字符串**完全不存在**（计数 0），所以是没有闸门，不是闸门写坏了。

### 10.3 `ready_service` 的等待

字符串 `the service was found among those described` 位于 `0x1262b8`，引用点 `0x717490`（失败分支打印）。同一函数在 `0x7173xx` 处以 **`0x80` 字节步进**遍历服务描述，命中后调 `0x7125f0` 处理——即“在服务目录里逐个找自己描述的接口”。main 同样具备该机制，差别在 0.3.7 的配置把 NFC→eSE→weaver 的依赖链**声明出来**，而我们的最小配置没有声明。

### 10.4 亮度回退

0.3.7 内 `normal maximum` 只剩一条打印用字面量（`0x1a5c6`），**没有** `gives its normal maximum as none of` 的致命分支。其背光路径在 `0x30c5c0`（`max_brightness`/`brightness` 的 sysfs 读写序列）之后直接进入显示流程，未出现“算不出就返回 Err”的形态。我们当前实现是“查不到就退回整段背光（`Ok(max)`）”，与它“不再致命”的取向一致；具体回退数值没有常量证据，标注为**推断**。

### 10.5 尚欠的一项

函数级二进制 diff（用指令指纹对齐两版，列出所有被改函数）尚未做。已知的语义差异已由上表与第 9 节覆盖，此项属验证性工作，不阻塞下一步。

---

*本报告只汇总已有实测与既有文档，不含新的设备写入操作。*

## 11. 最终结论（v55 实测闭环，2026-11-16 16:03）

`skyward-dodge-v55-ourELF-load.img`：**我们自己的 ELF + 我们自己的 device.toml，解密成功**。日志见 `/sdcard/Skyward/logs/20261116-160336/recovery.log`。

### 11.1 可解密的最小配置

- `[device] os_version_prop = "ro.build.display.id"` —— 版本按“已装系统”的属性读
- `[firmware] partition = "modem"`、`[partitions] mounted = ["persist"]`
- 三个 NFC 服务（带 `ready_service`）+ `[[owners]] /dev/nq-nci` —— eSE 链
- `[modules] sources = [...]`、`load = ["oplus_bsp_synaptics_tcm2", "nxp-nci"]` —— 模块 3.68 秒加载完（旧版 58 秒）

日志佐证：`199 of the phone's own services` → key 链清单 → `omapi: eSE1 is connected` → `1 of the secure world agreed on the key…` → `/data` 打开、`users whose own files are still closed: []`。

### 11.2 我们的 ELF 需要保留的三处补丁

| 补丁 | 作用 |
|---|---|
| 注释解析（`//`、`/* */`） | init.rc 才读得出 199 个服务 |
| `normal_max` 非致命 + `oplus` 组合 | 屏幕点得亮 |
| `WEAVERS` 加 `(".nxp", Through::ElementService)` | weaver 认路，eSE 链成立 |

### 11.3 被实测推翻的结论（勿再引用）

1. “缺就绪闸门是解密失败主因” —— 错，v55 的 ELF 仍无闸门却解密成功。
2. “v43 黑屏说明我们的 ELF 解不开” —— 错，是 toml 结构不兼容（`Device` 6 字段 / `Wlan` 8 字段）。
3. “`load` 字段我们的 ELF 不认” —— 错，源码本就有，删掉它反而让 v54 失败。

### 11.4 产物

| 文件 | 状态 |
|---|---|
| `skyward-dodge-v55-ourELF-load.img` | **推荐**：我们的 ELF + 最小配置，可解密 |
| `skyward-dodge-v42-baseline.img` | 它的原版，可解密，带 WiFi 菜单 |
| `/workspace/skyward/our_min.toml` | v55 的配置原件 |

### 11.5 仍未解决

- **MTP**：`the port did not take file transfer: sys.usb.state is none after 5000 ms`；我们的 `Device` 没有 `mtp` 字段，配置层开不了。
- **WiFi**：我们的 `Wlan` 缺 `unblock` 字段；它那版 WiFi 也未证实可用。
- **震动参数**：`Haptics` 缺 `period` 字段。

## 12. MTP 打通（v57 实测）

### 12.1 两个真因

1. **`skyward-mtp.rc` 从未被 import。** `init.rc` 开头只有 `import /init.recovery.${ro.hardware}.rc` 与 `import /system/etc/init.aging.rc`，那份定义 MTP functionfs 与 `sys.usb.state` 的 rc 压根没被解析，于是 `set_mtp(true)` 后状态永远是 `none`。
2. **规则被多余条件挡住。** 它的 rc 里第一条是 `on property:sys.usb.config=mtp,adb && property:sys.usb.config_disable=0`，而这套里没人设置 `sys.usb.config_disable`，条件永不成立 → adbd 不启动 → `sys.usb.ffs.ready` 永远为 0 → 绑定规则不触发 → 5 秒超时。

日志判据（`/sdcard/Skyward/logs/…/recovery.log`）：

```
the port did not take file transfer: sys.usb.state is Some("none") after 5000 ms, not "mtp,adb"
```

`Some("none")` = 写状态的规则从未执行。

### 12.2 修法（纯 ramdisk，无需改代码/重编）

- `init.rc` 追加 `import /system/etc/init/skyward-mtp.rc`；
- `skyward-mtp.rc` 第一行的 `&& property:sys.usb.config_disable=0` 去掉。

产物：`skyward-dodge-v57-mtp2.img`（SHA256 `5bd3ae0e1ac378bca31ea4427a00780428aee1f6e00a09f50ad1a25b96f55ca5`），实测 **MTP 可读、解密正常**。

### 12.3 现状

| 功能 | 状态 |
|---|---|
| 显示 / 触摸 / 解密 | ✅ |
| MTP 文件传输 | ✅ |
| 震动（默认频率） | 可用；`period` 回退补丁已给出，待编译 |
| WiFi | ❌ 源码无 `unblock` 实现 |

## 13. 自主版（v58）：底座换成原厂 + 上游，零第三方私有件

### 13.1 关键发现

把**原厂 recovery.img**（本机 OTA 里的 `recovery.img`）解包，与第三方移植包逐文件比对：

| 项 | 数量 |
|---|---|
| 原厂 ramdisk 文件 | 962 |
| 移植包文件 | 973 |
| 原厂有、移植包没有 | **0** |
| 移植包新增 | 11 |
| 移植包修改 | 3（`prop.default`、`system/bin/recovery`、`init.rc`） |

结论：**第三方移植包 = 原厂 ramdisk + 一套 Skyward 文件，未替换任何原厂文件。**

### 13.2 v58 的构成（无第三方私有件）

| 组件 | 来源 |
|---|---|
| ramdisk 962 个文件 | 原厂 recovery.img |
| `skyward/system-init`、字体、`task_profiles.json`、`skyward-mtp.rc` | 上游官方 v0.3.1（xuanyuan 包） |
| `skyward/device.toml` | 我们（`/workspace/skyward/our_min.toml`） |
| `system/bin/recovery` + 4 个符号链接 | 我们编译的 ELF（`1878904b…`） |
| `init.rc`（`start recovery` / `disabled` / `import skyward-mtp.rc`）、`prop.default`（去三行版本） | 我们在原厂文件上改的 |

产物：`skyward-dodge-v58-stockbase.img`，SHA256 `1b9b186793227f1bb88d394a54692f9a1c6e1923b6bc37354077fb2173036a26`。
实测：**显示、触摸、解密、MTP 全部正常**。

与移植版相比只剩 7 个文件不同：`device.toml`、`recovery`、`system-init`、`init.rc`、`skyward-mtp.rc`、`prop.default`、`system/bin/unzip`。

### 13.3 震动

**按机主意愿不启用**：`device.toml` 不含 `[haptics]`，Skyward 不驱动马达。
若将来需要，加回三行即可（`click` / `resonance` / `strength`）；`period` 需先给 `Haptics` 结构加字段并重编。

## 14. 双系统（Dual boot）实测记录

### 14.1 机制

Skyward 的“双系统”不是另建容器，而是**把一份完整 OTA 以快照（virtual A/B）方式装到另一个物理槽**：

- 数据落 `/data/gsi/ota/`（`*_cow-img.img` + `.0000`）；
- 快照登记在 `/metadata/gsi/ota/`（`lp_metadata` + 各分区 `.status`）；
- 切换只改写 `boot` / `init_boot` 指向；
- 合并（merge）由恢复侧执行；运行中的 ColorOS **没有 `snapshotctl`**，无法自行合并。

### 14.2 实测结果（ColorOS 同版本 OTA，装到 a 槽）

| 步骤 | 结果 |
|---|---|
| 设备校验 | 包 `pre-device=OP5D0DL1`，需把 `device.toml` 的 `codename` 与 `prop.default` 的 `ro.product.*.device` 都改成 OP5D0DL1，才通过 |
| 安装 | 成功，写入 9.2 GB 快照到 a 槽 |
| 启动 | 从 a 槽启动正常；版本、内核与 b 槽相同（共用 `/data` 与内核） |
| 合并 | **失败**：ColorOS 无 snapshotctl，恢复侧未提供可点的合并入口，状态永久停在“更新未写入完成” |
| 移除 | UI 无 Remove；手动清理三处后恢复 |

### 14.3 手动清除“第二系统”（需在**非该槽**运行时执行）

```sh
rm -rf /data/gsi/ota/*            # 9.2 GB 快照数据
rm -rf /metadata/gsi/ota/*        # 快照登记（lp_metadata / *.status）
rm -rf /metadata/ota/snapshots/*  # 分区快照表
rm -f  /metadata/ota/state        # ← 关键：74 B 的“有待合并更新”标记
rm -rf /data/misc/update_engine/prefs/*
rm -rf /data/gsi/dsu/*
```

漏掉 `/metadata/ota/state` 时，UI 会一直显示“更新未写入完成”。

### 14.4 结论

- **双系统机制本身可用**（能装、能启动、重启后保持在目标槽）；
- **HyperOS 装不进**：其 `product` 6 GB / `system_ext` 1.5 GB 远超本机分区（0.16 GB / 0.93 GB），且包内缺 `boot`、`vendor_dlkm`，只能整机线刷（会清 data）；
- A/B 双槽**共享 `/data`**，所以“装了别的系统也看到同样的应用”属正常。

## 15. 可复现构建（一键重出）

`/workspace/skyward/build_our.py` —— 一条命令从**原厂 recovery.img** 重出整包：

```bash
python3 /workspace/skyward/build_our.py
```

执行步骤（脚本内固化）：

1. 解原厂 `recovery.img` 的 ramdisk（LZ4 legacy → cpio）；
2. 放入上游官方 `skyward/`（`system-init`、两款字体）；
3. 写入我们的 `device.toml`（`codename=OP5D0DL1`、`os_version_prop`、`[modules] load`、NFC 三服务、`owners`）；
4. 放我们的 ELF（`1878904b…`，含注释解析 / 亮度兜底 / `.nxp` weaver 三补丁）＋ 四个指向 recovery 的符号链接；
5. 放上游 `task_profiles.json` 与 `skyward-mtp.rc`（去掉 `config_disable` 条件）；
6. 改原厂 `init.rc`：`start recovery`、`service recovery` 加 `disabled`、import MTP 规则；
7. 改原厂 `prop.default`：`ro.product.*.device=OP5D0DL1`、删三行版本属性；
8. 重打包（boot v4 + LZ4 legacy + AVB footer 重算），输出 `skyward-dodge-our.img`。

产物：`skyward-dodge-our.img`，SHA256 `dbc2ce2de43af9e5c71b555f2cb5707eac70f79128337c4a7e935f67398b4e9c`，ramdisk 75,395,454 B，ramdisk 内 1,084 个文件。

实测：**解密正常**。

底座构成：原厂 ramdisk + 上游官方资源 + 我们的 ELF/配置 —— **零第三方私有件**。

## 16. 待办（下次继续）

| 项 | 说明 | 需要 |
|---|---|---|
| About 页署名 | 在“设备显示/关于”页作者行后面追加 `初音 · deepseek · chatgpt` | 定位 `crates/skyward/src/ui.rs` 里 About 页那行字符串 → 改源码 → 重编 |
| DSU 快捷入口 | 镜像内加 `dsu-sticky.sh`：`gsi_tool enable` + 重启进 DSU | 改 `build_our.py`，无需重编 ELF |
| WiFi | `Wlan` 加 `unblock` 字段并实现 rfkill 解除 | 从零写代码 |
| MTP 复测 | `our.img` 已含两处修复，待插线验证 | 一台电脑 |
