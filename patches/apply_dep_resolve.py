#!/usr/bin/env python3
"""让模块依赖可以跨目录解析。

一加 13 上 `qca_cld3_peach_v2.ko` 在 `/vendor_dlkm/lib/modules`，而它依赖的
`rfkill.ko` 在 `/system_dlkm/lib/modules`；原实现只在本目录里解析依赖，于是报
“rfkill.ko is listed in /vendor_dlkm/lib/modules/modules.dep but missing”。

本补丁：本目录解析不到时，把同一次加载用到的所有目录再扫一遍。

用法： python3 apply_dep_resolve.py ~/skyward-main
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
LIB = ROOT / 'crates/skyward-modules/src/lib.rs'

old = '''            let path = graph
                .resolve_path(&m)
                .ok_or_else(|| anyhow!("{m} is listed in {}/modules.dep but missing", dir.display()))?;'''

new = '''            // 依赖未必与本模块同目录：一加 13 的 rfkill.ko 在 system_dlkm，
            // 而 qca_cld3_peach_v2.ko 在 vendor_dlkm。本目录找不到时扫全部目录。
            let path = graph
                .resolve_path(&m)
                .or_else(|| dirs.iter().map(|d| d.join(&m)).find(|p| p.is_file()))
                .ok_or_else(|| anyhow!("{m} is listed in {}/modules.dep but missing", dir.display()))?;'''

t = LIB.read_text(encoding='utf-8')
if 'or_else(|| dirs.iter()' in t:
    print('skip (already): M2 跨目录依赖解析')
else:
    n = t.count(old)
    assert n == 1, 'M2: expected 1 match, found %d' % n
    LIB.write_text(t.replace(old, new), encoding='utf-8')
    print('patched: M2 跨目录依赖解析')

print('done. 然后：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
