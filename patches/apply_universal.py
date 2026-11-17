#!/usr/bin/env python3
"""通用化补丁：把三处“固件变体一律当成死路”的判定改成尽量读、退化处理。

目标不是猜密码，而是：同一个包在别的固件版本上，不要因为一个 layout 号、
一个字节长度、一个厂商文件名不认识就整体放弃。

三处：
  U1 crates/skyward-crypto/src/user.rs   —— weaver 槽记录的 layout/长度/字节序
  U2 crates/skyward-crypto/src/keystore.rs —— keystore 目录的 version 号
  U3 crates/skyward-programs/src/secure.rs —— 不认识的 weaver 厂商走 secure element

幂等：已经打过就跳过。
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/workspace/skyward/dodgesrc")

PATCHES = []


def sub(path: str, old: str, new: str, marker: str):
    PATCHES.append((path, old, new, marker))


# ── U1 weaver slot ─────────────────────────────────────────────────────────
sub(
    "crates/skyward-crypto/src/user.rs",
    '''pub fn weaver_slot(raw: &[u8]) -> Result<u32> {
    let [layout, number @ ..] = raw else { bail!("the record of the blob's weaver slot is empty") };
    ensure!(*layout == WEAVER_SLOT_LAYOUT, "the blob's weaver slot is recorded in layout {layout}, and Skyward reads layout {WEAVER_SLOT_LAYOUT}");
    let Ok(number) = <[u8; 4]>::try_from(number) else {
        bail!("the record of the blob's weaver slot is {} bytes, and one is 5", raw.len());
    };
    let slot = i32::from_be_bytes(number);
    u32::try_from(slot).map_err(|_| anyhow::anyhow!("the blob's weaver slot is recorded as {slot}"))
}''',
    '''pub fn weaver_slot(raw: &[u8]) -> Result<u32> {
    let [layout, number @ ..] = raw else { bail!("the record of the blob's weaver slot is empty") };
    // 别的固件版本可能写成另一个 layout 号、另一种长度，甚至另一种字节序。
    // 这里不再因为“不认识这一版”整体放弃：照读数字，读不出来再退。
    if *layout != WEAVER_SLOT_LAYOUT {
        eprintln!("the blob's weaver slot is recorded in layout {layout}, and Skyward reads layout {WEAVER_SLOT_LAYOUT}: reading the number anyway");
    }
    let number = match <[u8; 4]>::try_from(number) {
        Ok(n) => n,
        Err(_) => {
            ensure!(raw.len() >= 4, "the record of the blob's weaver slot is {} bytes, and one is 5", raw.len());
            eprintln!("the record of the blob's weaver slot is {} bytes, and one is 5: reading the last four", raw.len());
            <[u8; 4]>::try_from(&raw[raw.len() - 4..]).expect("four bytes")
        }
    };
    for slot in [i32::from_be_bytes(number), i32::from_le_bytes(number)] {
        if slot >= 0 {
            return Ok(slot as u32);
        }
    }
    let slot = i32::from_be_bytes(number);
    Err(anyhow::anyhow!("the blob's weaver slot is recorded as {slot}"))
}''',
    "reading the number anyway",
)

sub(
    "crates/skyward-crypto/src/user.rs",
    '''        assert!(weaver_slot(&[2, 0, 0, 0, 5]).is_err(), "another layout");
        assert!(weaver_slot(&[1, 0, 0, 5]).is_err(), "cut short");''',
    '''        assert_eq!(weaver_slot(&[2, 0, 0, 0, 5]).unwrap(), 5, "another layout is read anyway");
        assert_eq!(weaver_slot(&[1, 0, 0, 5]).unwrap(), 5, "cut short: the last four are read");''',
    "another layout is read anyway",
)

# ── U2 keystore 目录布局号 ─────────────────────────────────────────────────
sub(
    "crates/skyward-crypto/src/keystore.rs",
    '''        if version.trim() != VERSION {
            bail!("{} is written in layout {}, Skyward reads layout {VERSION}", dir.display(), version.trim());
        }''',
    '''        if version.trim() != VERSION {
            // 别的 layout 号不再当死路：按已知的文件名照读，读不动再由下游报错。
            eprintln!("{} is written in layout {}, Skyward reads layout {VERSION}: reading it anyway", dir.display(), version.trim());
        }''',
    "reading it anyway",
)

sub(
    "crates/skyward-crypto/src/keystore.rs",
    '''    fn a_directory_in_an_older_layout_is_refused() {''',
    '''    fn a_directory_in_another_layout_is_read_anyway() {''',
    "a_directory_in_another_layout_is_read_anyway",
)

sub(
    "crates/skyward-crypto/src/keystore.rs",
    '''        fs::write(dir.join(FN_VERSION), "2").unwrap();
        assert!(refusal(&dir).contains("layout 2"), "{}", refusal(&dir));''',
    '''        fs::write(dir.join(FN_VERSION), "2").unwrap();
        assert!(StoredKey::load(&dir).is_ok(), "another layout is read anyway");''',
    "another layout is read anyway",
)

# ── U3 不认识的 weaver 厂商 ───────────────────────────────────────────────
sub(
    "crates/skyward-programs/src/secure.rs",
    '''    let (_, through) = WEAVERS.iter().find(|(ending, _)| file.ends_with(ending)).with_context(|| format!("{} is a weaver of a make the recovery does not know", program.path))?;''',
    '''    let through = match WEAVERS.iter().find(|(ending, _)| file.ends_with(ending)) {
        Some((_, through)) => through,
        None => {
            eprintln!("{file} is a weaver of a make the recovery does not know; trying the secure element anyway");
            &Through::ElementService
        }
    };''',
    "trying the secure element anyway",
)


def main() -> int:
    failed = 0
    for path, old, new, marker in PATCHES:
        p = ROOT / path
        text = p.read_text(encoding="utf-8")
        if marker in text:
            print(f"已打过，跳过 {path}: {marker[:30]}")
            continue
        if old not in text:
            print(f"!! 找不到原文 {path}: {old.splitlines()[0][:60]!r}")
            failed += 1
            continue
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"已改 {path}: {marker[:40]}")
    print("失败项:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
