#!/usr/bin/env python3
"""让 wpa_supplicant 的输出落到文件里。

它现在以 `Stdio::null()` 启动，退出码 1 时看不到任何原因。改成写
`/skyward/run/supplicant/wpa_supplicant.log`，下次失败就能读到它说了什么。

用法： python3 apply_supplicant_log.py ~/skyward-main
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
WLAN = ROOT / 'crates/skyward/src/wlan.rs'

old = '''        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())'''

new = '''        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))
        .stderr(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))'''

helper = '''
/// 打开（或创建）supplicant 的日志文件：它退出时留下的输出是判断原因的唯一线索。
fn log_file(path: &std::path::Path) -> Result<fs::File> {
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("open {}", path.display()))
}
'''

t = WLAN.read_text(encoding='utf-8')
if 'fn log_file' in t:
    print('skip (already): S1 supplicant 日志')
else:
    n = t.count(old)
    assert n == 1, 'S1: expected 1 match, found %d' % n
    t = t.replace(old, new)
    t = t.rstrip('\n') + '\n' + helper
    WLAN.write_text(t, encoding='utf-8')
    print('patched: S1 supplicant 输出落盘')

print('done. 然后：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
