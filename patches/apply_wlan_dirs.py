#!/usr/bin/env python3
"""回退全局模块目录改动，改为**只给 WLAN** 追加 system_dlkm 目录。

上一版把 `source_dir("system_dlkm")` 全局指向 `/system_dlkm/lib/modules`，
结果那 99 个内核模块也跟着被加载，与 vendor_dlkm 的版本冲突，key 链断掉、
data 解不开。本补丁：

1. 把 `source_dir()` 恢复成上游原样（全局不动，解密安全）；
2. 只在启动 WLAN 工作线程时，把 `/system_dlkm/lib/modules` 追加进搜索路径，
   这样 `rfkill.ko` 找得到，而其它流程完全不受影响。

用法： python3 apply_wlan_dirs.py ~/skyward-main
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
LIB = ROOT / 'crates/skyward-modules/src/lib.rs'
UI = ROOT / 'crates/skyward/src/ui.rs'

# ---------------------------------------------------------------- 1. 回退全局改动
patched = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
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

original = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(match source {
        "vendor_boot" => "/lib/modules",
        "vendor_dlkm" => "/vendor_dlkm/lib/modules",
        "vendor" => "/vendor/lib/modules",
        "system_dlkm" => "/system_dlkm/flatten/lib/modules",
        _ => bail!("unknown module source {source:?}"),
    }))
}'''

t = LIB.read_text(encoding='utf-8')
if patched in t:
    LIB.write_text(t.replace(patched, original), encoding='utf-8')
    print('reverted: M1 全局目录改动')
else:
    print('skip: M1 不在（已是原样）')

# ---------------------------------------------------------------- 2. 只给 WLAN 追加目录
old = '            let modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;'
new = '''            let mut modules = cfg.modules.sources.iter().map(|s| skyward_modules::source_dir(s)).collect::<Result<Vec<_>>>()?;
            // 一加 13 把内核自己的模块放在 /system_dlkm/lib/modules（没有 flatten 那层）。
            // 只给 WLAN 追加这个目录：rfkill.ko 在那里，而全局的模块加载保持原样，
            // 免得 system_dlkm 的模块与 vendor_dlkm 的版本冲突、把 key 链弄断。
            let system_dlkm = std::path::PathBuf::from("/system_dlkm/lib/modules");
            if system_dlkm.is_dir() && !modules.contains(&system_dlkm) {
                modules.push(system_dlkm);
            }'''

u = UI.read_text(encoding='utf-8')
if 'system_dlkm.is_dir()' in u:
    print('skip: W5 WLAN 目录追加（已打）')
else:
    n = u.count(old)
    assert n == 1, 'W5: expected 1 match, found %d' % n
    UI.write_text(u.replace(old, new), encoding='utf-8')
    print('patched: W5 只给 WLAN 追加 system_dlkm')

print('done. 然后：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
