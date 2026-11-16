#!/usr/bin/env python3
"""一加 13（dodge）Skyward 移植：全部补丁，一次打完（幂等）。

用法：
    python3 apply_all.py ~/skyward-main
    cargo ndk -t arm64-v8a --platform 30 build --release -p skyward
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
CFG = ROOT / 'crates/skyward-config/src/lib.rs'
UI = ROOT / 'crates/skyward/src/ui.rs'
SECURE = ROOT / 'crates/skyward-programs/src/secure.rs'
SCRIPT = ROOT / 'crates/skyward-programs/src/script.rs'
MODULES = ROOT / 'crates/skyward-modules/src/lib.rs'
WLAN = ROOT / 'crates/skyward/src/wlan.rs'


def has(path, needle):
    return needle in path.read_text(encoding='utf-8')


def edit(path, old, new, tag):
    t = path.read_text(encoding='utf-8')
    n = t.count(old)
    assert n == 1, '%s: expected 1 match, found %d' % (tag, n)
    path.write_text(t.replace(old, new), encoding='utf-8')
    print('patched:', tag)


def append(path, text, tag):
    t = path.read_text(encoding='utf-8')
    path.write_text(t.rstrip('\n') + '\n' + text, encoding='utf-8')
    print('patched:', tag)


# ---------------------------------------------------------------- P1 注释解析
if has(SCRIPT, 'fn strip_comments'):
    print('skip: P1')
else:
    edit(SCRIPT,
         'pub fn read(text: &str) -> Result<Script> {\n    let mut script = Script::default();',
         'pub fn read(text: &str) -> Result<Script> {\n    let text = &strip_comments(text);\n    let mut script = Script::default();',
         'P1a read() 剥离')
    append(SCRIPT, '''
/// 去掉 `//` 行注释与 `/* ... */` 块注释。
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
''', 'P1b strip_comments')

# ---------------------------------------------------------------- P2 亮度
if has(UI, 'oplus,dsi-bl-normal-max-level'):
    print('skip: P2')
else:
    edit(UI, 'const NORMAL_MAX: [(&str, &str); 3] = [',
             'const NORMAL_MAX: [(&str, &str); 4] = [', 'P2a 容量')
    edit(UI, '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
             '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n'
             '    ("oplus,dsi-bl-normal-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
             'P2b oplus 组')
    edit(UI, 'fn normal_max(max: u32) -> Result<u32> {',
             'fn normal_max_strict(max: u32) -> Result<u32> {', 'P2c strict')
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

# ---------------------------------------------------------------- P3 weaver
if has(SECURE, '".nxp", Through::ElementService'):
    print('skip: P3')
else:
    edit(SECURE,
         'const WEAVERS: [(&str, Through); 3] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),',
         'const WEAVERS: [(&str, Through); 4] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),\n    (".nxp", Through::ElementService),',
         'P3 weaver .nxp')

# ---------------------------------------------------------------- W1..W3 WLAN
if has(CFG, 'pub unblock: bool'):
    print('skip: W1')
else:
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
}''', 'W1 Wlan.unblock')

if has(WLAN, 'fn unblock_radio'):
    print('skip: W2/W3')
else:
    edit(WLAN, '    for w in &cfg.writes {',
              '    if cfg.unblock {\n        unblock_radio();\n    }\n    for w in &cfg.writes {',
              'W2 调用 unblock')
    append(WLAN, '''
/// 把每个 rfkill 的软封锁写 0。
fn unblock_radio() {
    if let Ok(entries) = std::fs::read_dir("/sys/class/rfkill") {
        for e in entries.flatten() {
            let soft = e.path().join("soft");
            if soft.exists() { let _ = std::fs::write(&soft, "0"); }
        }
    }
}
''', 'W3 unblock_radio')

# ---------------------------------------------------------------- W4 回退全局目录
patched_dir = '        "system_dlkm" => &["/system_dlkm/lib/modules", "/system_dlkm/flatten/lib/modules"],'
original_dir = '        "system_dlkm" => "/system_dlkm/flatten/lib/modules",'
mt = MODULES.read_text(encoding='utf-8')
if patched_dir in mt:
    # 把整个改过的函数还原
    start = mt.index('pub fn source_dir(')
    end = mt.index('\n}\n', start) + 3
    MT_ORIG = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(match source {
        "vendor_boot" => "/lib/modules",
        "vendor_dlkm" => "/vendor_dlkm/lib/modules",
        "vendor" => "/vendor/lib/modules",
        "system_dlkm" => "/system_dlkm/flatten/lib/modules",
        _ => bail!("unknown module source {source:?}"),
    }))
}
'''
    MODULES.write_text(mt[:start] + MT_ORIG + mt[end:], encoding='utf-8')
    print('reverted: W4 全局 system_dlkm')
else:
    print('skip: W4')

# ---------------------------------------------------------------- W5 只给 WLAN 加目录
if has(UI, 'system_dlkm.is_dir()'):
    print('skip: W5')
else:
    edit(UI,
         '            let modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;',
         '''            let mut modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;
            // 一加 13 的内核模块在 /system_dlkm/lib/modules；只给 WLAN 追加，
            // 全局加载保持原样，免得与 vendor_dlkm 冲突断了 key 链。
            let system_dlkm = std::path::PathBuf::from("/system_dlkm/lib/modules");
            if system_dlkm.is_dir() && !modules.contains(&system_dlkm) {
                modules.push(system_dlkm);
            }''', 'W5 WLAN 追加 system_dlkm')

# ---------------------------------------------------------------- M2 依赖跨目录
if has(MODULES, 'or_else(|| dirs.iter()'):
    print('skip: M2')
else:
    edit(MODULES,
         '            let path = graph\n                .resolve_path(&m)',
         '            // 依赖未必同目录：rfkill.ko 在 system_dlkm，qca_cld3_peach_v2.ko 在 vendor_dlkm。\n'
         '            let path = graph\n                .resolve_path(&m)\n'
         '                .or_else(|| dirs.iter().map(|d| d.join(&m)).find(|p| p.is_file()))',
         'M2 依赖跨目录')

# ---------------------------------------------------------------- S1 supplicant 日志
if has(WLAN, 'fn log_file'):
    print('skip: S1')
else:
    edit(WLAN,
         '        .stdin(Stdio::null())\n        .stdout(Stdio::null())\n        .stderr(Stdio::null())',
         '        .stdin(Stdio::null())\n'
         '        .stdout(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))\n'
         '        .stderr(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))',
         'S1 supplicant 输出落盘')
    append(WLAN, '''
/// supplicant 的日志：它退出时留下的输出是判断原因的唯一线索。
fn log_file(path: &std::path::Path) -> Result<fs::File> {
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("open {}", path.display()))
}
''', 'S1 log_file')

print('\n全部完成。接着：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
