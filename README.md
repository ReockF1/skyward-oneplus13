# Skyward for OnePlus 13 (dodge / PJZ110)

把 [Skyward Recovery](https://github.com/kirklin/skyward) 移植到一加 13 中国版（`PJZ110` / 代号 `dodge`）的构建与补丁集。

> 这不是一份完整源码树，而是**可复现的移植层**：上游源码 + 三处补丁 + 本机配置 + 打包脚本。
> 底座取自**本机原厂 `recovery.img`** 与**上游官方发布包**，不含任何第三方私有二进制。

## 状态

| 功能 | 状态 |
|---|---|
| 显示 / 触摸 | ✅ |
| data 解密（FBE） | ✅ |
| MTP 文件传输 | ✅ |
| 震动 | ⚪ 默认关闭（配置未启用 `[haptics]`） |
| WiFi | ⚠️ 可用但不稳定（扫描 / WPA2·WPA3 / DHCP / 联网 / 7～10 MB/s 下载均已实测；存在随机内核 panic，见“已知问题”） |

## 设备事实

| 项 | 值 |
|---|---|
| 型号 | OnePlus 13 中国版 `PJZ110` |
| 代号 / 设备串 | `dodge` / `OP5D0DL1` |
| 系统 | ColorOS 17 / Android 17，内核 6.6.118 |
| **底座版本** | **PJZ110 17.0.0.103(CN01)**（原厂 `recovery.img` 取自该系统） |
| 面板 | `qcom,mdss_dsi_panel_AA569_P_3_A0019_dsc_cmd`（cmd，DSC） |
| boot 头 | v4，`kernel_size=0`，ramdisk LZ4 legacy |

## 三处源码补丁

`patches/apply.py` 会对一份 skyward 源码树打三处补丁（均带锚点断言）：

1. **注释解析** `crates/skyward-programs/src/script.rs`
   init.rc 里的 `//` 与 `/* */` 注释会让解析在 `begins no section` 处早停，只读到 4 个服务。补上剥离逻辑后读到 199 个。
2. **亮度不再致命** `crates/skyward/src/ui.rs`
   `normal_max()` 只查 `mi,` 三组属性；一加 13 面板只有 `oplus,dsi-bl-normal-max-level`，断言失败即退出 → 黑屏。补 `oplus` 组，并把失败降级为“退回整段背光”。
3. **weaver 认路** `crates/skyward-programs/src/secure.rs`
   `WEAVERS` 增加 `(".nxp", Through::ElementService)`，否则 `android.hardware.weaver-service.nxp` 被判“不认识的 make”，key 链中止。

```bash
python3 patches/apply.py ~/skyward-main
cd ~/skyward-main && cargo ndk -t arm64-v8a --platform 30 build --release -p skyward
```

## 配置要点（`config/dodge.toml`）

解密能否成立，取决于这两块：

```toml
[device]
os_version_prop = "ro.build.display.id"   # 按“已装系统”的属性读版本

[modules]
sources = ["vendor_boot", "vendor_dlkm", "vendor", "system_dlkm"]
load = ["oplus_bsp_synaptics_tcm2", "nxp-nci"]   # 模块提前到 ~3.7s 加载
```

另需：`[firmware] partition="modem"`、`[partitions] mounted=["persist"]`、三个带 `ready_service` 的 NFC 服务、`[[owners]] /dev/nq-nci`。

> 注意：`codename` 与设备串不同。Skyward 安装包校验读的是**属性**（`ro.product.*.device`），所以本仓库的镜像会把 ramdisk `prop.default` 一并改成 `OP5D0DL1`。

## 构建

`build/build_our.py` 一条命令从原厂镜像重出整包：

```bash
python3 build/build_our.py
```

依赖：`lz4`、`cpio`、python3；一份原厂 `recovery.img`、一份上游发布包的 ramdisk（提供 `skyward/system-init` 与字体）、以及打过补丁的 ELF。

流程：解原厂 ramdisk → 放入上游 `skyward/` 资源与我们的 `device.toml` → 放 ELF 与四个符号链接 → 补 `task_profiles.json` 与 `skyward-mtp.rc`（去掉永不成立的 `sys.usb.config_disable` 条件）→ 改 `init.rc`（`start recovery` / `disabled` / import）→ 改 `prop.default`（设备串、删三行版本属性）→ 重打包（boot v4 + LZ4 + AVB footer 重算）。

## 已知问题

- **WiFi 会随机 panic（当前最大阻塞项）**：功能已打通（扫描 46 BSS、WPA2/WPA3 四次握手、DHCP、联网、`dl` 下载实测 7.4～10.3 MB/s，最大一次下到 1.84 GB），但存在两类**内核 panic**：①连接瞬间（48～50 秒）；②连上后约 300 秒。两者都已确认是 `kernel crash`（`/proc/last_boot_reason`），但恢复环境里拿不到 panic 文本（`pstore` 无 console 记录、`rawdump` 全 0、minidump 的动态栈未启用）。
- **WiFi 库依赖**：必须让 `wpa_supplicant` 跑在**恢复自己的 `/system/lib64`** 上（`libraries = ["/system/lib64", "/vendor/lib64"]`，与上游 21 个机型一致）。顺序反过来会拉进厂商 `libbinder.so`，它依赖恢复里没有的 `libapexsupport.so`。
- **最低电量要求**：电量约 40%（3.68 V）时射频校准起不来（日志里连 `Calibration took` 都没有），建议 60% 以上或插充电器。
- **双系统**：可把完整 OTA 装到另一槽并启动，但合并需恢复侧执行；运行中的 ColorOS 无 `snapshotctl`，会长期停在“更新未写入完成”。清理方法见 `docs/port-report.md` 第 14 节。
- **HyperOS**：无法装入现有分区（`product` 需 5.94 GB，本机仅 0.16 GB），只能整机线刷。

## 许可与致谢

- 本项目是 [kirklin/skyward](https://github.com/kirklin/skyward) 的衍生移植，遵循 **GPL-3.0-or-later**，见 `LICENSE`。
- 上游作者与 Kirk Mono 字体作者的署名保留在二进制与关于页中。
- 移植所得结论与踩坑记录见 `docs/port-report.md`。
