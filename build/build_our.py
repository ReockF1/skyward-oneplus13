#!/usr/bin/env python3
"""一键重出我们的 Skyward 恢复镜像（可复现构建）

输入：原厂 recovery.img（本机 OTA 里的）
底座：原厂 ramdisk + 上游官方 skyward 资源 + 我们的 ELF 与配置
输出：skyward-dodge-our.img
"""
import os, shutil, struct, subprocess, hashlib, tempfile, sys

STOCK_IMG = '/workspace/mounts/Download/一加13 Skyward rec移植/当前系统的原包镜像/recovery.img'
UPSTREAM  = '/workspace/up/root/skyward'          # 上游官方包里的 skyward/ 目录
OUR_ELF   = '/storage/emulated/0/Download/UURemote/YUQIUONO/skyward'
OUR_TOML  = '/workspace/skyward/our_min.toml'
OUT_DIR   = '/workspace/mounts/Download/一加13 Skyward rec移植'
NAME      = 'skyward-dodge-our.img'
IMG_SIZE  = 104857600

def run(*a, **kw):
    subprocess.run(a, check=True, **kw)

def unpack(img, root):
    d = open(img, 'rb').read()
    assert d[:8] == b'ANDROID!', '不是 android boot 镜像'
    rd_size = struct.unpack_from('<I', d, 12)[0]
    rd = d[4096:4096 + rd_size]
    work = tempfile.mkdtemp(prefix='ourbuild_')
    raw = os.path.join(work, 'rd.bin'); cpio = os.path.join(work, 'rd.cpio')
    open(raw, 'wb').write(rd)
    if rd[:4] == b'\x02\x21\x4c\x18':
        run('lz4', '-d', '-f', raw, cpio)
    elif rd[:2] == b'\x1f\x8b':
        run('sh', '-c', 'gzip -dc %s > %s' % (raw, cpio))
    else:
        shutil.copyfile(raw, cpio)
    os.makedirs(root, exist_ok=True)
    run('sh', '-c', 'cpio -idmu --no-absolute-filenames < %s' % cpio, cwd=root)
    return d[:4096], d[-64:]

def main():
    tree = '/workspace/skyward/build_our'
    shutil.rmtree(tree, ignore_errors=True)
    hdr, footer = unpack(STOCK_IMG, tree)

    # 1) 上游 skyward 资源
    os.makedirs(os.path.join(tree, 'skyward'), exist_ok=True)
    for f in ('KirkMono-OFL.txt', 'KirkMono-Regular.ttf', 'MiSansVF.ttf', 'system-init'):
        shutil.copyfile(os.path.join(UPSTREAM, f), os.path.join(tree, 'skyward', f))
    os.chmod(os.path.join(tree, 'skyward', 'system-init'), 0o755)

    # 2) 我们的配置
    shutil.copyfile(OUR_TOML, os.path.join(tree, 'skyward/device.toml'))

    # 3) 我们的 ELF + 符号链接
    elf = os.path.join(tree, 'system/bin/recovery')
    shutil.copyfile(OUR_ELF, elf); os.chmod(elf, 0o755)
    for n in ('7za', 'avbctl', 'resetprop', 'zstd'):
        p = os.path.join(tree, 'system/bin', n)
        if os.path.lexists(p): os.remove(p)
        os.symlink('recovery', p)

    # 4) 上游的 task_profiles.json 与 MTP 规则（去掉那个永不成立的条件）
    shutil.copyfile('/workspace/up/root/system/etc/task_profiles.json',
                    os.path.join(tree, 'system/etc/task_profiles.json'))
    mtp_src = '/workspace/up/root/system/etc/init/skyward-mtp.rc'
    mtp = open(mtp_src, encoding='utf-8').read().replace(' && property:sys.usb.config_disable=0', '')
    open(os.path.join(tree, 'system/etc/init/skyward-mtp.rc'), 'w', encoding='utf-8').write(mtp)

    # 5) init.rc：启动目标、service disabled、import
    ip = os.path.join(tree, 'system/etc/init/hw/init.rc')
    s = open(ip, encoding='utf-8', errors='surrogateescape').read()
    s = s.replace('    start phoenix_recovery', '    start recovery', 1)
    if 'skyward-mtp.rc' not in s:
        s = s.replace('import /system/etc/init.aging.rc',
                      'import /system/etc/init.aging.rc\nimport /system/etc/init/skyward-mtp.rc', 1)
    s = s.replace('service recovery /system/bin/recovery\n',
                  'service recovery /system/bin/recovery\n    disabled\n', 1)
    open(ip, 'w', encoding='utf-8', errors='surrogateescape').write(s)

    # 6) prop.default：设备名 + 去掉三行版本属性
    pp = os.path.join(tree, 'prop.default')
    lines = open(pp, encoding='utf-8', errors='surrogateescape').read().splitlines(True)
    out = []
    for l in lines:
        if l.startswith(('ro.build.version.release=', 'ro.build.version.security_patch=',
                         'ro.vendor.build.security_patch=')):
            continue
        for part in ('system', 'vendor', 'odm', 'product', 'system_ext'):
            if l.startswith('ro.product.%s.device=ossi' % part):
                l = 'ro.product.%s.device=OP5D0DL1\n' % part
        out.append(l)
    if not any(x.startswith('ro.product.device=') for x in out):
        out.append('ro.product.device=OP5D0DL1\n')
    open(pp, 'w', encoding='utf-8', errors='surrogateescape').write(''.join(out))

    # 7) 重打包
    cpio = '/workspace/skyward/build_our.cpio'; lz4 = '/workspace/skyward/build_our.lz4'
    with open(cpio, 'wb') as f:
        run('sh', '-c', 'find . -print0 | cpio --null -o -H newc --owner=root:root --quiet',
            cwd=tree, stdout=f)
    run('lz4', '-l', '-9', '-f', cpio, lz4)
    rd = open(lz4, 'rb').read()
    h = bytearray(hdr); struct.pack_into('<I', h, 12, len(rd))
    magic, vmaj, vmin, ois, vbo, vbs = struct.unpack_from('>4sIIQQQ', footer, 0)
    vbmeta = open(STOCK_IMG, 'rb').read()[vbo:vbo + vbs]
    end = (4096 + len(rd) + 4095) // 4096 * 4096
    img = bytearray(IMG_SIZE)
    img[0:4096] = h
    img[4096:4096 + len(rd)] = rd
    img[end:end + len(vbmeta)] = vbmeta
    img[IMG_SIZE - 64:] = struct.pack('>4sIIQQQ', b'AVBf', vmaj, vmin, end, end, len(vbmeta)) + footer[36:]
    path = os.path.join(OUT_DIR, NAME)
    open(path, 'wb').write(img)
    print('输出: %s' % path)
    print('大小: %d B' % os.path.getsize(path))
    print('SHA256: %s' % hashlib.sha256(img).hexdigest())
    print('ELF: %s' % hashlib.sha256(open(elf, 'rb').read()).hexdigest())
    print('ramdisk: %d B' % len(rd))

if __name__ == '__main__':
    main()
