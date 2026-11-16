#!/usr/bin/env python3
"""给一份 Skyward 源码树加上一加 13 的 WLAN 支持（unblock）。

上游没有 `unblock` 字段，也没有解除 rfkill 封锁的代码：驱动起来后无线电仍被
软封锁，wpa_supplicant 扫不到任何网络。这个补丁补两处：

1. `skyward-config`：`Wlan` 增加 `unblock: bool`（serde 默认 false）；
2. `skyward`：`bring_up()` 在写完 `writes` 之后，把所有 rfkill 的 `soft` 写 0。

用法： python3 apply_wlan.py ~/skyward-main
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
CFG = ROOT / 'crates/skyward-config/src/lib.rs'
WLAN = ROOT / 'crates/skyward/src/wlan.rs'


def edit(path, old, new, tag):
    t = path.read_text(encoding='utf-8')
    if new in t:
        print('skip (already):', tag)
        return
    n = t.count(old)
    assert n == 1, '%s: expected 1, found %d' % (tag, n)
    path.write_text(t.replace(old, new), encoding='utf-8')
    print('patched:', tag)


# 1) 配置字段
edit(CFG,
     '''    /// The page the stock system asks over plain HTTP to tell whether a
    /// network reaches the internet, which answers 204 with nothing.
    pub probe: String,
}''',
     '''    /// The page the stock system asks over plain HTTP to tell whether a
    /// network reaches the internet, which answers 204 with nothing.
    pub probe: String,
    /// Unblock the radio once the driver is in: the driver leaves it soft
    /// blocked until its firmware is up, which a recovery reaches later
    /// than the system does.
    #[serde(default)]
    pub unblock: bool,
}''', 'W1 Wlan.unblock 字段')

# 2) bring_up 里解除封锁
old_tail = '''    for w in &cfg.writes {'''
new_block = '''    if cfg.unblock {
        unblock_radio();
    }
    for w in &cfg.writes {'''
edit(WLAN, old_tail, new_block, 'W2 bring_up 调用 unblock')

helper = '''
/// 把每个 rfkill 的软封锁写 0：驱动起来后无线电仍是 soft blocked，
/// 不解除的话 supplicant 扫不到任何网络。
fn unblock_radio() {
    if let Ok(entries) = std::fs::read_dir("/sys/class/rfkill") {
        for e in entries.flatten() {
            let soft = e.path().join("soft");
            if soft.exists() {
                let _ = std::fs::write(&soft, "0");
            }
        }
    }
}
'''
t = WLAN.read_text(encoding='utf-8')
if 'fn unblock_radio' not in t:
    WLAN.write_text(t.rstrip('\n') + '\n' + helper, encoding='utf-8')
    print('patched: W3 unblock_radio 实现')

print('done. 然后：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
