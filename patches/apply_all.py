#!/usr/bin/env python3
"""一加 13（dodge）Skyward 移植：全部补丁，一次打完。

用法：
    python3 apply_all.py ~/skyward-main
    cargo ndk -t arm64-v8a --platform 30 build --release -p skyward

包含：
  P1 注释解析（init.rc 的 // 与 /* */）
  P2 亮度不再致命（oplus 属性 + 失败降级）
  P3 weaver 认 .nxp
  W1 Wlan.unblock 字段
  W2 bring_up 里解除 rfkill 封锁
  W3 unblock_radio 实现
  W4 回退“全局 system_dlkm 目录”（若曾打过）
  W5 只给 WLAN 追加 /system_dlkm/lib/modules
  M2 模块依赖跨目录解析
  S1 wpa_supplicant 输出落盘
全部幂等：已打过的会打印 skip。
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
CFG = ROOT / 'crates/skyward-config/src/lib.rs'
UI = ROOT / 'crates/skyward/src/ui.rs'
SECURE = ROOT / 'crates/skyward-programs/src/secure.rs'
SCRIPT = ROOT / 'crates/skyward-programs/src/script.rs'
MODULES = ROOT / 'crates/skyward-modules/src/lib.rs'
WLAN = ROOT / 'crates/skyward/src/wlan.rs'


def edit(path, old, new, tag):
    t = path.read_text(encoding='utf-8')
    if new in t:
        print('skip:', tag)
        return
    n = t.count(old)
    assert n == 1, '%s: expected 1 match, found %d' % (tag, n)
    path.write_text(t.replace(old, new), encoding='utf-8')
    print('patched:', tag)


# ------------------------------------------------------------------ P1 注释解析
if 'fn strip_comments' in SCRIPT.read_text(encoding='utf-8'):
    print('skip: P1 注释解析')
else:
    edit(SCRIPT,
         'pub fn read(text: &str) -> Result<Script> {\n    let mut script = Script::default();',
         'pub fn read(text: &str) -> Result<Script> {\n    let text = &strip_comments(text);\n    let mut script = Script::default();',
         'P1a read() 调用剥离')
    helper = '''
/// 去掉 `//` 行注释与 `/* ... */` 块注释：一加 13 的 init.rc 里两者都有，
/// 原来的解析遇到它们会在 `begins no section` 处早停。
fn strip_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    let mut line_start = true;
    while let Some(c) = chars.next() {
        if c == '/' && chars.peek() == Some(&'*') {
            chars.next();
            while let Some(d) = chars.next() {
                if d == '*' && chars.peek() == Some(&'/') { chars.next(); break; }
            }
            continue;
        }
        if line_start && c == '/' && chars.peek() == Some(&'/') {
            while let Some(d) = chars.next() {
                if d == '\\n' { out.push(d); break; }
            }
            continue;
        }
        line_start = c == '\\n';
        out.push(c);
    }
    out
}
'''
    t = SCRIPT.read_text(encoding='utf-8')
    SCRIPT.write_text(t.rstrip('\n') + '\n' + helper, encoding='utf-8')
    print('patched: P1b strip_comments')

# ------------------------------------------------------------------ P2 亮度
edit(UI, 'const NORMAL_MAX: [(&str, &str); 3] = [',
         'const NORMAL_MAX: [(&str, &str); 4] = [', 'P2a NORMAL_MAX 容量')
edit(UI, '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
         '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n'
         '    ("oplus,dsi-bl-normal-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
         'P2b oplus 组')
edit(UI, 'fn normal_max(max: u32) -> Result<u32> {',
         'fn normal_max_strict(max: u32) -> Result<u32> {', 'P2c 改名 strict')
wrapper = '''/// 面板的常规最大亮度；面板不以本恢复读取的方式给出它时，整段背光顶上。
fn normal_max(max: u32) -> Result<u32> {
    match normal_max_strict(max) {
        Ok(level) => Ok(level),
        Err(e) => {
            eprintln!("{e:#}; using the whole backlight range instead");
            Ok(max)
        }
    }
}

'''
edit(UI, '/// Leave recovery for the chosen target.\nfn power(',
         wrapper + '/// Leave recovery for the chosen target.\nfn power(', 'P2d 非致命包装')

# ------------------------------------------------------------------ P3 weaver
edit(SECURE,
     'const WEAVERS: [(&str, Through); 3] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),',
     'const WEAVERS: [(&str, Through); 4] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),\n    (".nxp", Through::ElementService),',
     'P3 weaver .nxp')

# ------------------------------------------------------------------ W1..W3 WLAN
edit(CFG,
     '''    /// The page the stock system asks over plain HTTP to tell whether a
    /// network reaches the internet, which answers 204 with nothing.
    pub probe: String,
}''',
     '''    /// The page the stock system asks over plain HTTP to tell whether a
    /// network reaches the internet, which answers 204 with nothing.
    pub probe: String,
    /// Unblock the radio once the driver is in.
    #[serde(default)]
    pub unblock: bool,
}''', 'W1 Wlan.unblock 字段')

edit(WLAN, '    for w in &cfg.writes {',
          '    if cfg.unblock {\n        unblock_radio();\n    }\n    for w in &cfg.writes {',
          'W2 bring_up 调用 unblock')

if 'fn unblock_radio' not in WLAN.read_text(encoding='utf-8'):
    t = WLAN.read_text(encoding='utf-8')
    WLAN.write_text(t.rstrip('\n') + '''
/// 把每个 rfkill 的软封锁写 0。
fn unblock_radio() {
    if let Ok(entries) = std::fs::read_dir("/sys/class/rfkill") {
        for e in entries.flatten() {
            let soft = e.path().join("soft");
            if soft.exists() { let _ = std::fs::write(&soft, "0"); }
        }
    }
}
''', encoding='utf-8')
    print('patched: W3 unblock_radio')
else:
    print('skip: W3 unblock_radio')

# ------------------------------------------------------------------ W4 回退全局目录
patched_dir = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
    // 一加 13 把内核自己的模块放在 /system_dlkm/lib/modules，
    // 而不是上游假设的 /system_dlkm/flatten/lib/modules。
    let candidates: &[&str] = match source {
        "vendor_boot" => &["/lib/modules"],
        "vendor_dlkm" => &["/vendor_dlkm/lib/modules"],
        "vendor" => &["/vendor/lib/modules"],
        "system_dlkm" => &["/system_dlkm/lib/modules", "/system_dlkm/flatten/lib/modules"],
        _ => bail!("unknown module source {source:?}"),
    };
    if let Some(found) = candidates.iter().map(Path::new).find(|p| p.is_dir()) {
        return Ok(found.to_path_buf());
    }
    Ok(PathBuf::from(candidates[0]))
}'''
original_dir = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(match source {
        "vendor_boot" => "/lib/modules",
        "vendor_dlkm" => "/vendor_dlkm/lib/modules",
        "vendor" => "/vendor/lib/modules",
        "system_dlkm" => "/system_dlkm/flatten/lib/modules",
        _ => bail!("unknown module source {source:?}"),
    }))
}'''
mt = MODULES.read_text(encoding='utf-8')
if patched_dir in mt:
    MODULES.write_text(mt.replace(patched_dir, original_dir), encoding='utf-8')
    print('reverted: W4 全局 system_dlkm 目录')
else:
    print('skip: W4（已是原样）')

# ------------------------------------------------------------------ W5 只给 WLAN 加目录
edit(UI,
     '            let modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;',
     '''            let mut modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;
            // 一加 13 的内核模块在 /system_dlkm/lib/modules（没有 flatten 那层）。
            // 只给 WLAN 追加，全局加载保持原样，免得与 vendor_dlkm 版本冲突断了 key 链。
            let system_dlkm = std::path::PathBuf::from("/system_dlkm/lib/modules");
            if system_dlkm.is_dir() && !modules.contains(&system_dlkm) {
                modules.push(system_dlkm);
            }''', 'W5 只给 WLAN 追加 system_dlkm')

# ------------------------------------------------------------------ M2 依赖跨目录
edit(MODULES,
     '''            let path = graph
                .resolve_path(&m)''',
     '''            // 依赖未必与本模块同目录：rfkill.ko 在 system_dlkm，qca_cld3_peach_v2.ko 在 vendor_dlkm。
            let path = graph
                .resolve_path(&m)
                .or_else(|| dirs.iter().map(|d| d.join(&m)).find(|p| p.is_file()))''',
     'M2 依赖跨目录')

# ------------------------------------------------------------------ S1 supplicant 日志
edit(WLAN,
     '''        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())''',
     '''        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))
        .stderr(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))''',
     'S1 supplicant 输出落盘')

if 'fn log_file' not in WLAN.read_text(encoding='utf-8'):
    t = WLAN.read_text(encoding='utf-8')
    WLAN.write_text(t.rstrip('\n') + '''
/// supplicant 的日志：它退出时留下的输出是判断原因的唯一线索。
fn log_file(path: &std::path::Path) -> Result<fs::File> {
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("open {}", path.display()))
}
''', encoding='utf-8')
    print('patched: S1 log_file')
else:
    print('skip: S1 log_file')

print('\n全部完成。接着：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
