# Skyward Recovery · 一加 13 WLAN 稳定性 · 交接给外部审阅的报告（v4）

- 时间：2026-11-18 04:45
- 设备：OnePlus 13 / `OP5D0DL1` / `PJZ110` / ColorOS 17 / 内核 `6.6.118-android15-8-g9bc34d5b0c79`
- 恢复环境：Skyward（自编 Rust 程序，跑在厂商 recovery ramdisk 上）
- 槽位 `_b`，恢复分区 `/dev/block/by-name/recovery_b`
- 本报告只讲**一个主题**：WiFi 已经能用，但会随机**内核 panic**，需要外部判断根因方向

---

## 1. 已经确定的事实（可直接采信，均有原始日志）

### 1.1 功能层面：WiFi 完全可用

| 项 | 证据 |
|---|---|
| 驱动/固件/校准 | `cnss: Calibration took 8248 ms`、`htc_wait_target: Target Ready!`、`Firmware build version : 10097fff` |
| 扫描 | `scan id 40961 type COMPLETED(1) reason COMPLETED(1) scan found 46 bss` |
| WPA2/WPA3 连接 | `wlan0(vdevid-0): … Connect with <ap> is SU…`，UI 显示 864 Mbps / 第 6 代 / WPA2/WPA3-Personal |
| DHCP + 联网 | `wlan0 has 10.28.116.73 … through 10.28.116.213`、`the network on wlan0 reaches the internet` |
| 吞吐 | 实测 **7.4～10.3 MB/s**（单连接 HTTP 下载） |
| 恢复内下载 | 自研 `dl`（`recovery fetch` 的壳），支持 `.part` 断点续传；3.81 GB 的包下到 **1.84 GB** 时被打断 |

### 1.2 崩溃：确认是内核 panic，且有两类

```
/proc/last_boot_reason → "kernel crash Fulldump [2026-11-18 04:14:14]"
```

| 类别 | 时间点（自开机） | 当时在做什么 | 日志特征 |
|---|---|---|---|
| **A 类** | 48～50 s | 扫描完成、点“连接”的瞬间 | 无 `rcu stall`、无 `hung_task`、无 `Auth TX`；日志在 `pmo_core_enable_wow_in_fw` 之后约 2 秒截断 |
| **B 类** | 297～302 s | 连上后空闲或正在下载 | 之前抓到过 `hung_task … __scm_smc_call`（TEE/SCM 卡死）、`ipa_pm_set_throughput: Invalid Params` → `RM CONS set perf profile failed: 16` |

B 类四次全部落在 297.7～302.3 秒：v85(299.76)、v93(297.70)、v100(302.27)、v103(297.70)。

### 1.3 排除项（都已做过对照实验）

| 排除的假设 | 怎么排除的 |
|---|---|
| 库顺序/命名空间导致 | v103 完全按上游 21 个机型的做法（`libraries=["/system/lib64","/vendor/lib64"]`、不写 ld.config），崩点不变 |
| 下载负载导致 | 主动下载（v100/v103）与纯空闲（v85/v93）崩点相同 |
| 调试脚手架导致 | v91 把挂 `/metadata`、spawn sh、改 ini 全部撤掉，照样崩 |
| 关 WoW/SAP 挂起导致 | v86 起关闭，v90 撤回，两者都崩 |
| 需求预先解密 | 解密与不解密两种情况都崩 |
| 只是“恢复环境 300 秒自爆” | **v105 验证：不点 WiFi 静置 8 分钟以上，正常，不崩** |

### 1.4 取证手段：全部失效

| 手段 | 结果 |
|---|---|
| `pstore` | 恢复里挂载成功（`ramoops: --- pstore contents --- total 0`），Android 侧 `/sys/fs/pstore` 也是空 |
| `qcom_dynamic_ramoops` | **模块已加载**（`ramoops already loaded`，占用 12288 B），DT 里也有 `ramoops-region`，但 pstore 里始终没有 console 记录 |
| boot image cmdline 注入 | 已在 v4 头偏移 44 写入 `printk.always_kmsg_dump=1`，仍无 dump（怀疑 QCOM bootloader 不使用 header cmdline） |
| `rawdump` 分区 | 256 MB，**从头到尾全 0** |
| `logfs` 分区 | 8 MB FAT，挂载成功但**空** |
| `panic_on_oops=1` + `panic=20` | 仍无 dump（panic 发生了，只是没落点） |

---

## 2. 当前环境的关键差异（相对正常 Android 启动）

> 这些是**事实描述**，不是结论。它们可能是崩溃的土壤。

| 项 | 恢复环境里的情况 |
|---|---|
| thermal | `cnss: Failed to get cooling device node`（两次）；没有 thermal HAL / thermal-engine |
| 电源/性能 | 没有 perf HAL、没有功耗策略；PMIC 由内核直接管 |
| TEE | qseecomd / keymint / ssgtzd 是 Skyward 为“解密”临时拉起的，没人做生命周期管理 |
| 时间同步 | `hdd_tsf_is_initialized: TSF is not initialized`（连接前后反复出现） |
| MAC | `cnss_utils: WLAN MAC address is not set, type 0` → `using default MAC address`（persist 里没有 `wlan_mac.bin`） |
| DMS | `cnss: DMS QMI connection not established`（大量刷屏，约 43 秒后自行停止） |
| IPA | `ipa_pm ipa_pm_set_throughput:1313 Invalid Params` → `ipa_wdi_set_perf_profile_per_inst:973 fail` → `dp_ipa_set_perf_level … code -14` → `wlan_ipa_set_perf_level: RM CONS set perf profile failed: 16` |
| selinux | permissive（大量 `avc: denied … permissive=1`） |
| 未知设备节点 | `peach_v2: hdd_config_power: power attr and opm attr is null`、`hdd_sysfs_bmiss_create: could not create bmiss sysfs file` |

---

## 3. 已做过的版本对照（只列最近、有判据价值的）

| 版本 | 关键改动 | 结果 |
|---|---|---|
| v82 | 命名空间方案（ld.config.txt） | **首次连接成功**：864 Mbps + IP + 联网 |
| v100 | 前 60 秒 200 ms 落盘 + 保活 | 下载 523 MB 后 302 s 崩 |
| v103 | **改回上游库顺序**（恢复自己的 `/system/lib64` 在前），不写命名空间 | 下载 **1.84 GB** 后 297.7 s 崩 |
| v105 | 恢复一开始就启动落盘与内核观测 | 不点 WiFi 静置 8 分钟**不崩** |
| v108 | `/metadata` 被 Skyward 挂成只读（实测 105 次 EROFS）→ 写失败就重挂 | 日志不再断 |
| v109 | 显式 `insmod` + 挂 pstore + 抄内容 | 确认 ramoops 链路的死点（见 §1.4） |

---

## 4. 想请外部判断的四个问题

1. **A 类（连接瞬间 panic，48～50 s）**：在“recovery、无 thermal/无 perf HAL、SELinux permissive”的环境里，QCA `qca_cld3_peach_v2` + `cnss2` 在 nl80211 连接（`AUTH` 之前）就 panic 的已知成因有哪些？是否与 `hdd_config_power: power attr and opm attr is null`、缺失 cooling device、或 `TSF is not initialized` 有直接关系？

2. **B 类（约 300 秒）**：122 秒起 `adci_thread`/`cpudmof` 卡在 `__scm_smc_call`，242 秒报 hung_task，297～302 秒 panic；同时 `ipa_pm_set_throughput` 在崩溃前 1 秒报 `Invalid Params`。这条链更像 **TEE 失联**、**IPA 状态机崩坏**、还是 **RCU stall + `panic_on_rcu_stall`**？如何用不改内核的手段区分？

3. **取证**：在 `pstore` 不保存 console（`qcom_dynamic_ramoops` 只做 minidump）、`rawdump` 全 0、bootloader 忽略 header cmdline 的情况下，有没有办法拿到 panic 文本？（已知选项：写内核 panic-notifier 模块 / 用 EDL 抓 minidump / 别的机型是怎么做的？）

4. **策略**：要不要干脆放弃厂商 `wpa_supplicant` + `cnss2` 这条链，改用自编译的 `wpa_supplicant`（只依赖系统库）来避开厂商 HAL 的耦合？有没有其他 recovery 项目在骁龙 8 Elite 上这么干过？

---

## 5. 代码与产物位置

- 源码：`/workspace/skyward/dodgesrc`，HEAD `53d9219`（本地，未推送）
- 关键提交：`00b7ead`（下载入口/dl/保活/cnss-daemon/库顺序改回上游）、`a4b8d16`（命名空间，后来被 v103 弃用）、`6abfe03`（落盘失败上报）、`d27aa74`（只读重挂）、`53d9219`（ramoops 探针）
- 镜像：`/sdcard/Download/一加13 Skyward rec移植/skyward-dodge-v109-ramoops.img`（`4f70d077…`）等 6 张
- 全量日志样本：`/metadata/skyward/wlan-diag/tail.log`、`/sdcard/Skyward/wlan-diag/tail.log`（1500 行过滤窗口，前 60 秒 200 ms 一写）

---

## 6. 一句话总结

> WiFi 功能已经完全可用（扫描、WPA2/WPA3、DHCP、联网、7～10 MB/s），但存在两类**内核 panic**：连接瞬间（48～50 s）与约 300 秒。已排除库配置、下载负载、调试脚手架、WoW 开关、是否解密；现有手段（pstore/ramoops/rawdump/cmdline）都拿不到 panic 文本。下一步是定向二分特性，或投入做内核 panic-notifier 模块换取 dump。
