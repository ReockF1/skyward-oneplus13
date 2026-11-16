#!/usr/bin/env python3
"""给一份 Skyward 源码树打一加 13（dodge）移植所需的三处补丁。

用法： python3 apply.py ~/skyward-main
每一处都带锚点断言，打不中会直接报错停止，不会悄悄改坏。
重复执行是安全的（已打过会跳过）。
"""
import pathlib, sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path.home() / 'skyward-main')
SCRIPT = ROOT / 'crates/skyward-programs/src/script.rs'
UI = ROOT / 'crates/skyward/src/ui.rs'
SECURE = ROOT / 'crates/skyward-programs/src/secure.rs'


def edit(path, old, new, tag):
    t = path.read_text(encoding='utf-8')
    if new in t:
        print('skip (already):', tag)
        return
    n = t.count(old)
    assert n == 1, '%s: expected 1 match, found %d' % (tag, n)
    path.write_text(t.replace(old, new), encoding='utf-8')
    print('patched:', tag)


# ---------------------------------------------------------------- 1. 注释解析
anchor = 'pub fn read(text: &str) -> Result<Script> {\n    let mut script = Script::default();'
repl = ('pub fn read(text: &str) -> Result<Script> {\n'
        '    let text = &strip_comments(text);\n'
        '    let mut script = Script::default();')
if 'fn strip_comments' in SCRIPT.read_text(encoding='utf-8'):
    print('skip (already): P1 注释解析')
else:
    edit(SCRIPT, anchor, repl, 'P1a read() 调用剥离')
    helper = '''
/// 去掉 `//` 行注释与 `/* ... */` 块注释：一加 13 的 init.rc 里两者都有，
/// 原来的解析遇到它们会在 `begins no section` 处早停，只读到少数几个服务。
fn strip_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    let mut line_start = true;
    while let Some(c) = chars.next() {
        if c == '/' && chars.peek() == Some(&'*') {
            chars.next();
            while let Some(d) = chars.next() {
                if d == '*' && chars.peek() == Some(&'/') {
                    chars.next();
                    break;
                }
            }
            continue;
        }
        if line_start && c == '/' && chars.peek() == Some(&'/') {
            while let Some(d) = chars.next() {
                if d == '\\n' {
                    out.push(d);
                    break;
                }
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
    print('patched: P1b strip_comments 助手')

# ---------------------------------------------------------------- 2. 亮度兜底
edit(UI, 'const NORMAL_MAX: [(&str, &str); 3] = [',
         'const NORMAL_MAX: [(&str, &str); 4] = [', 'P2a NORMAL_MAX 容量')

edit(UI, '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
         '    ("mi,mdss-dsi-fac-bl-max-level", "qcom,mdss-dsi-bl-max-level"),\n'
         '    ("oplus,dsi-bl-normal-max-level", "qcom,mdss-dsi-bl-max-level"),\n];',
         'P2b oplus 组')

edit(UI, 'fn normal_max(max: u32) -> Result<u32> {',
         'fn normal_max_strict(max: u32) -> Result<u32> {', 'P2c 改名 strict')

wrapper = '''/// 面板的常规最大亮度。面板不以本恢复读取的方式给出它时，整段背光顶上：
/// 屏幕照样点亮，只是永远不会要求高亮模式。
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

# ---------------------------------------------------------------- 3. weaver 认路
old = 'const WEAVERS: [(&str, Through); 3] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),'
new = 'const WEAVERS: [(&str, Through); 4] = [\n    (".thales", Through::ElementService),\n    (".nxp-qti", Through::ElementService),\n    (".nxp", Through::ElementService),'
edit(SECURE, old, new, 'P3 weaver .nxp')

print('done. 接下来：cargo ndk -t arm64-v8a --platform 30 build --release -p skyward')
