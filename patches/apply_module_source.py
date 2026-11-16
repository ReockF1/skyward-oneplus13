#!/usr/bin/env python3
"""让 `system_dlkm` 的模块目录按实际布局查找。

上游假设内核自己的模块在 `/system_dlkm/flatten/lib/modules`，而一加 13 上它们在
`/system_dlkm/lib/modules`（`flatten` 目录不存在），于是 `rfkill.ko` 找不到，
WLAN 开关直接报 “rfkill.ko is in none of […]”。

本补丁把 `source_dir()` 改成：先找 `/system_dlkm/lib/modules`，没有再退回
`flatten` 那一套；顺带对每个来源都只在目录存在时采用。

用法： python3 apply_module_source.py ~/skyward-main
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
LIB = ROOT / 'crates/skyward-modules/src/lib.rs'

old = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
    Ok(PathBuf::from(match source {
        "vendor_boot" => "/lib/modules",
        "vendor_dlkm" => "/vendor_dlkm/lib/modules",
        "vendor" => "/vendor/lib/modules",
        "system_dlkm" => "/system_dlkm/flatten/lib/modules",
        _ => bail!("unknown module source {source:?}"),
    }))
}'''

new = '''pub fn source_dir(source: &str) -> Result<PathBuf> {
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

t = LIB.read_text(encoding='utf-8')
if '/system_dlkm/lib/modules' in t:
    print('skip (already): M1 system_dlkm 目录回退')
else:
    n = t.count(old)
    assert n == 1, 'M1: expected 1 match, found %d' % n
    LIB.write_text(t.replace(old, new), encoding='utf-8')
    print('patched: M1 system_dlkm 目录回退')

print('done. 然后：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
