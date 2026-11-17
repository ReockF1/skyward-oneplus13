//! The phone's WLAN inside the recovery, on a thread of its own: the driver
//! brought up, the phone's own wpa_supplicant joining networks, and an
//! address asked for over DHCP and kept. The screen is told how things
//! stand each time that changes.

use anyhow::{bail, ensure, Context, Result};
use skyward_config::{Firmware, Wlan as Config};
use skyward_net::dhcp::{self, Kept, Lease};
use skyward_net::http::Servers;
use skyward_net::iface;
use skyward_net::reach::{self, Reach};
use skyward_net::supplicant::{self, Bss, Control, Event as Told, Security};
use skyward_net::wlan::{self, Joined, Link, Next, Power, Stage, Traffic, Wlan};
use skyward_sys::worker::{self, Teller};
use std::fs;
use std::io::Write;
use std::net::Ipv4Addr;
use std::os::fd::RawFd;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command as Process, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

/// The first folder the firmware loader looks in that a recovery can
/// write.
const FIRMWARE: &str = "/etc/firmware";

/// Where a partition a firmware file is read from is mounted meanwhile.
const PARTITIONS: &str = "/skyward/wlan";

/// The supplicant's settings, and the folder of its control socket, in the
/// recovery service's own folder.
const HOME: &str = "/skyward/run/supplicant";


/// Where the last lines of the kernel log are kept while the WLAN comes up.
/// 未加密的 /metadata 排在前面：它不过 data 那套加解密，休眠前后更稳。
const MIRROR: [&str; 2] = ["/metadata/skyward/wlan-diag", "/sdcard/Skyward/wlan-diag"];

/// How many of the kernel log's last lines are kept there.
const MIRROR_LINES: usize = 1500;

/// 几路刷屏的东西：充电、审计、触摸、看门狗、以及 cnss 等 DMS 时每 56 毫秒
/// 一条的那句。落盘时丢掉，免得把真正要看的几行挤出去。
const MIRROR_NOISE: [&str; 15] = [
    "OPLUS_CHG",
    "audit: type=1400",
    "si-mo:",
    "tri_state_key",
    "syna_",
    "[wdog_util]",
    "iostack:",
    // 连上之后驱动会一直打漫游扫描的样表，几秒钟就把窗口填满；
    // 注意别把 [schedu] 整个滤掉——扫描和连接的行也是这个前缀。
    "CMN_MLME",
    "ROAM INIT INFO",
    "ROAM_SCAN",
    // 系统侧的周期性噪声
    "healthd:",
    "black_screen_check",
    "bright_screen_check",
    "[OPLUS_WD]",
    "wcd_usbss",
];

/// Whether the log is already being kept.
static MIRRORED: AtomicBool = AtomicBool::new(false);

/// Whether the link is already being kept warm.
static KEEPING: AtomicBool = AtomicBool::new(false);

/// Keep the kernel log's tail on the phone's own storage, refreshed every
/// second, so a reset that never reaches the kernel's own crash dump still
/// leaves the last second of what the driver said behind.
fn mirror_kernel_log() {
    if MIRRORED.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(|| {
        let mut kept: Vec<PathBuf> = Vec::new();
        let mut sdcard = fs::metadata("/sdcard").ok().map(|m| m.dev());
        let mut last = Instant::now();
        let mut beat = Instant::now();
        let started = Instant::now();
        loop {
            // 解密之前 /sdcard 只是 ramdisk 里的一层壳；设备号一变就重新认一遍。
            let now = fs::metadata("/sdcard").ok().map(|m| m.dev());
            if now != sdcard || kept.is_empty() {
                sdcard = now;
                kept = reopen_mirror();
                for dir in &kept {
                    let _ = skyward_sys::kmsg(&format!("wlan: keeping the kernel log in {}", dir.display()));
                }
            }
            if let Ok(text) = skyward_sys::kernel_log() {
                let lines: Vec<&str> = text.lines().filter(|line| !MIRROR_NOISE.iter().any(|noise| line.contains(noise))).collect();
                let from = lines.len().saturating_sub(MIRROR_LINES);
                let body = lines[from..].join("\n");
                let mut wrote = false;
                for dir in &kept {
                    wrote |= save_log(dir, &body);
                }
                if wrote {
                    // 休眠会把这条线程冻住；醒来第一件事就是把缺口记下来，别让断档无声无息。
                    let gap = last.elapsed().as_secs();
                    if gap > 5 {
                        let _ = skyward_sys::kmsg(&format!("wlan: the log went unwritten for {gap} s"));
                    }
                    last = Instant::now();
                }
            }
            if beat.elapsed().as_secs() >= 20 {
                beat = Instant::now();
                let _ = skyward_sys::kmsg(&format!("wlan: mirror beat, {} folders hold the log", kept.len()));
            }
            // 前 60 秒抓得密一点：崩溃都在 supplicant 起来后的几秒里。
            let every = if started.elapsed().as_secs() < 60 { 200 } else { 1000 };
            thread::sleep(Duration::from_millis(every));
        }
    });
}

/// 未解密时 /sdcard 只是 ramdisk 里的一层壳，日志无处可写；把 /metadata 挂成

/// 目录项也要落盘：只 sync 文件的话，断电后 rename 出来的名字会丢。
fn sync_dir(dir: &Path) {
    if let Ok(folder) = fs::File::open(dir) {
        let _ = folder.sync_all();
    }
}

/// 未解密时 /sdcard 只是 ramdisk 里的一层壳；/metadata 是 f2fs 明文分区，
/// 把它挂成可写，日志就能活过重启，不用先解 /data。
fn mount_metadata_for_log() {
    let at = Path::new("/metadata");
    let folder = at.join("skyward/wlan-diag");
    let probe = folder.join("probe");
    if fs::create_dir_all(&folder).is_ok() && fs::write(&probe, b"").is_ok() {
        sync_dir(&folder);
        return;
    }
    if skyward_block::mount::is_mount_point(at).unwrap_or(false) {
        if let Err(e) = skyward_block::mount::umount(at) {
            let _ = skyward_sys::kmsg(&format!("wlan: cannot unmount /metadata: {e:#}"));
        }
    }
    let device = Path::new(skyward_block::BY_NAME).join("metadata");
    match skyward_block::mount::mount(&device, at, "f2fs", &[]) {
        Ok(()) => {
            let _ = skyward_sys::kmsg("wlan: /metadata is mounted writable for the log");
            if fs::create_dir_all(&folder).is_ok() {
                sync_dir(&folder);
            }
        }
        Err(e) => {
            let _ = skyward_sys::kmsg(&format!("wlan: cannot mount {}: {e:#}", device.display()));
        }
    }
}

/// 认一遍能写的落盘目录，认出来的每个都留着。
fn reopen_mirror() -> Vec<PathBuf> {
    mount_metadata_for_log();
    let mut out = Vec::new();
    for at in MIRROR {
        let dir = Path::new(at);
        if fs::create_dir_all(dir).is_ok() && fs::write(dir.join("probe"), b"").is_ok() {
            sync_dir(dir);
            out.push(dir.to_path_buf());
        }
    }
    out
}

/// 先写 tmp、sync、再改名：崩在中间也留下完整的一份。
fn save_log(dir: &Path, body: &str) -> bool {
    let tmp = dir.join("tail.tmp");
    let Ok(mut file) = fs::File::create(&tmp) else { return false };
    if file.write_all(body.as_bytes()).is_err() {
        return false;
    }
    let _ = file.sync_all();
    drop(file);
    let moved = fs::rename(&tmp, dir.join("tail.log")).is_ok();
    if moved {
        sync_dir(dir);
    }
    moved
}

/// The user the supplicant takes on as it starts, who has to be able to
/// make its socket.
const WIFI: u32 = 1010;

/// How long the driver may take to make its interface, the chip's firmware
/// loading meanwhile.
const INTERFACE_WAIT: Duration = Duration::from_secs(60);

/// How long the supplicant may take to open its control socket.
const SOCKET_WAIT: Duration = Duration::from_secs(10);

/// How long the supplicant gets to end once asked to.
const END_WAIT: Duration = Duration::from_secs(3);

/// How often a network joined is read for its signal.
const POLL: Duration = Duration::from_secs(5);

/// How often what went over the interface is counted, for how fast it
/// goes.
const TRAFFIC: Duration = Duration::from_secs(1);

/// How long after the network gave no address it is asked again.
const RETRY: Duration = Duration::from_secs(10);

/// What the screen asks of the WLAN.
pub enum Command {
    On,
    Off,
    Scan,
    Join { ssid: Vec<u8>, name: String, security: Security, password: String },
    Leave,
}

/// What comes to the thread: a command, how asking for an address went,
/// how far the network joined reaches, or that the screen has let the
/// thread go.
enum Inbox {
    Command(Command),
    Address(u64, Result<Asked>),
    Reach(u64, Reach),
    End,
}

enum Asked {
    Got(Lease),
    Kept(Kept),
}

/// What the thread tells the screen: how the WLAN stands, or how fast data
/// went over it in the last second.
enum Report {
    Wlan(Wlan),
    Traffic(Traffic),
}

pub struct Worker {
    /// What the thread is asked, each of which wakes it.
    inbox: Teller<Inbox>,
    /// What the screen is told. The thread is waited for once it has been
    /// asked to end.
    worker: worker::Worker<Report>,
}

impl Worker {
    /// Start the thread, with the WLAN off. `modules` are the folders the
    /// driver's modules are found in, `libraries` the bundle of the vendor
    /// interface's libraries, and `servers` where the DNS servers the
    /// network gives are kept for whoever looks names up.
    pub fn start(cfg: Config, modules: Vec<PathBuf>, libraries: Option<PathBuf>, servers: Servers) -> Result<Worker> {
        let (inbox, asked) = worker::telling().context("make the WLAN thread's inbox")?;
        let back = inbox.clone();
        let worker = worker::Worker::start("wlan", move |tell| {
            let mut s = Station { cfg, modules, libraries, servers, state: Wlan::default(), running: None, back, tell };
            s.run(&asked);
        })?;
        Ok(Worker { inbox, worker })
    }

    pub fn send(&self, c: Command) {
        self.inbox.tell(Inbox::Command(c));
    }

    /// Readable while the screen has something to be told.
    pub fn fd(&self) -> RawFd {
        self.worker.fd()
    }

    /// How the WLAN stands now, and how fast data went over it in the last
    /// second, each if told since the last call.
    pub fn told(&self) -> (Option<Wlan>, Option<Traffic>) {
        let (mut wlan, mut traffic) = (None, None);
        for report in self.worker.events() {
            match report {
                Report::Wlan(w) => wlan = Some(w),
                Report::Traffic(t) => traffic = Some(t),
            }
        }
        (wlan, traffic)
    }
}

impl Drop for Worker {
    /// The WLAN goes off with the thread.
    fn drop(&mut self) {
        self.inbox.tell(Inbox::End);
    }
}

/// The supplicant running, and the address the network gave.
struct Running {
    child: Child,
    control: Control,
    monitor: Control,
    mac: [u8; 6],
    found: Vec<Bss>,
    lease: Option<Held>,
    /// The thread asking for an address or to keep one, and its number;
    /// what an earlier one answers is let go.
    asking: Option<(u64, Arc<AtomicBool>)>,
    asked: u64,
    /// The number of the last look at how far the network reaches; what an
    /// earlier one finds is let go.
    probed: u64,
    /// When to ask for an address again after the network gave none.
    retry_at: Option<Instant>,
    polled_at: Instant,
    /// What had gone over the interface when last counted, while it has an
    /// address.
    counted: Option<Counted>,
}

/// The interface's totals in and out, in bytes, and when they were read.
#[derive(Clone, Copy)]
struct Counted {
    at: Instant,
    rx: u64,
    tx: u64,
}

/// A lease, when it was given, and how far keeping it has come: 0 asks its
/// server at the renewal time, 1 any server at the rebinding time, 2 lets
/// it go when it runs out.
struct Held {
    lease: Lease,
    at: Instant,
    phase: u8,
}

impl Held {
    fn due(&self) -> Instant {
        let after = match self.phase {
            0 => self.lease.renew,
            1 => self.lease.rebind,
            _ => self.lease.seconds,
        };
        self.at + Duration::from_secs(after.into())
    }
}

struct Station {
    cfg: Config,
    modules: Vec<PathBuf>,
    libraries: Option<PathBuf>,
    servers: Servers,
    state: Wlan,
    running: Option<Running>,
    /// What a thread asking for an address, or looking how far the
    /// network reaches, answers to: the thread's own inbox.
    back: Teller<Inbox>,
    tell: Teller<Report>,
}

impl Station {
    fn run(&mut self, asked: &worker::Told<Inbox>) {
        let mut told = self.state.clone();
        loop {
            let mut fds = vec![libc::pollfd { fd: asked.fd(), events: libc::POLLIN, revents: 0 }];
            if let Some(r) = &self.running {
                fds.push(libc::pollfd { fd: r.monitor.fd(), events: libc::POLLIN, revents: 0 });
            }
            let timeout = self.next().map_or(-1, |at| at.saturating_duration_since(Instant::now()).as_millis().min(60_000) as i32);
            if unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) } < 0 {
                let e = std::io::Error::last_os_error();
                assert_eq!(e.kind(), std::io::ErrorKind::Interrupted, "poll the WLAN thread's inputs: {e}");
            }
            for message in asked.take() {
                match message {
                    Inbox::Command(c) => self.command(c),
                    Inbox::Address(n, answer) => self.addressed(n, answer),
                    Inbox::Reach(n, reach) => self.reached(n, reach),
                    Inbox::End => return self.off(),
                }
            }
            self.listen();
            self.timers();
            if self.state != told {
                told = self.state.clone();
                self.tell.tell(Report::Wlan(told.clone()));
            }
        }
    }

    /// The soonest a timer is due.
    fn next(&self) -> Option<Instant> {
        let r = self.running.as_ref()?;
        let poll = matches!(self.state.joined, Some(Joined { stage: Stage::Connected(_), .. })).then(|| r.polled_at + POLL);
        let count = r.lease.as_ref().map(|_| r.counted.map_or_else(Instant::now, |c| c.at + TRAFFIC));
        [r.lease.as_ref().filter(|_| r.asking.is_none()).map(Held::due), r.retry_at, poll, count].into_iter().flatten().min()
    }

    fn command(&mut self, c: Command) {
        match c {
            Command::On => self.on(),
            Command::Off => self.off(),
            Command::Scan => {
                if let Some(r) = &self.running {
                    // A scan under way answers FAIL-BUSY, and its results
                    // come all the same.
                    if let Err(e) = r.control.ask("SCAN") {
                        self.lost(e);
                    }
                }
            }
            Command::Join { ssid, name, security, password } => {
                self.unaddress();
                let mut joined = Joined { ssid, name, security, stage: Stage::Joining };
                if let Err(e) = self.join(&joined, &password) {
                    let _ = skyward_sys::kmsg(&format!("joining {} failed: {e:#}", joined.name));
                    joined.stage = Stage::Failed;
                }
                self.state.joined = Some(joined);
            }
            Command::Leave => {
                self.unaddress();
                if let Some(r) = &self.running
                    && let Err(e) = r.control.order("REMOVE_NETWORK all")
                {
                    self.lost(e);
                }
                self.state.joined = None;
            }
        }
    }

    fn on(&mut self) {
        if self.running.is_some() {
            return;
        }
        self.state.power = Power::Starting;
        self.tell.tell(Report::Wlan(self.state.clone()));
        match self.start() {
            Ok(r) => {
                self.running = Some(r);
                self.state.power = Power::On;
                self.command(Command::Scan);
            }
            Err(e) => {
                let _ = skyward_sys::kmsg(&format!("the WLAN did not come up: {e:#}"));
                self.state.power = Power::Failed(format!("{e:#}"));
            }
        }
    }

    fn start(&self) -> Result<Running> {
        bring_up(&self.cfg, &self.modules)?;
        let mut child = start_supplicant(&self.cfg, self.libraries.as_deref())?;
        let socket = Path::new(HOME).join("sockets").join(&self.cfg.interface);
        let connected = (|| -> Result<(Control, Control)> {
            let control = Control::open(&socket)?;
            ensure!(control.ask("PING")?.trim_end() == "PONG", "wpa_supplicant does not answer PING");
            let monitor = Control::open(&socket)?;
            monitor.attach()?;
            Ok((control, monitor))
        })();
        let (control, monitor) = match connected {
            Ok(both) => both,
            Err(e) => {
                end(&mut child, None);
                return Err(e);
            }
        };
        let mac = iface::hardware_address(&self.cfg.interface)?;
        Ok(Running {
            child,
            control,
            monitor,
            mac,
            found: Vec::new(),
            lease: None,
            asking: None,
            asked: 0,
            probed: 0,
            retry_at: None,
            polled_at: Instant::now(),
            counted: None,
        })
    }

    fn off(&mut self) {
        self.unaddress();
        if let Some(mut r) = self.running.take() {
            end(&mut r.child, Some(&r.control));
        }
        if let Err(e) = iface::set_up(&self.cfg.interface, false) {
            let _ = skyward_sys::kmsg(&format!("{e:#}"));
        }
        self.state = Wlan::default();
    }

    /// The supplicant no longer answers: the WLAN is off, and why is said.
    fn lost(&mut self, e: anyhow::Error) {
        let _ = skyward_sys::kmsg(&format!("the WLAN went away: {e:#}"));
        self.off();
        self.state.power = Power::Failed(format!("{e:#}"));
    }

    fn join(&self, joined: &Joined, password: &str) -> Result<()> {
        let r = self.running.as_ref().context("the WLAN is off")?;
        r.control.order("REMOVE_NETWORK all")?;
        let id: u32 = r.control.ask("ADD_NETWORK")?.trim().parse().context("wpa_supplicant numbered the network with no number")?;
        // One the last scan did not see may hide its name, as one added by
        // hand may.
        let hidden = !r.found.iter().any(|b| b.ssid == joined.ssid);
        for c in supplicant::network_commands(id, &joined.ssid, joined.security, password, hidden)? {
            r.control.order(&c)?;
        }
        r.control.order(&format!("SELECT_NETWORK {id}"))
    }

    /// Read what the supplicant has told.
    fn listen(&mut self) {
        let Some(r) = &self.running else { return };
        let messages = match r.monitor.messages() {
            Ok(m) => m,
            Err(e) => return self.lost(e),
        };
        for m in messages {
            let Some(told) = supplicant::event(&m) else { continue };
            match told {
                Told::ScanResults => {
                    if let Err(e) = self.read_scan() {
                        return self.lost(e);
                    }
                }
                Told::ScanFailed => {}
                Told::Terminating => return self.lost(anyhow::anyhow!("wpa_supplicant ended")),
                other => self.joining(&other),
            }
        }
    }

    fn read_scan(&mut self) -> Result<()> {
        let r = self.running.as_mut().expect("scan results come from a supplicant running");
        let mut found = Vec::new();
        let mut first = 0;
        loop {
            let page = supplicant::bss_list(&r.control.ask(&supplicant::bss_command(first))?)?;
            let Some(last) = page.last() else { break };
            first = last.id + 1;
            found.extend(page);
        }
        self.state.networks = wlan::networks(&found);
        r.found = found;
        Ok(())
    }

    fn joining(&mut self, told: &Told) {
        let Some(j) = &mut self.state.joined else { return };
        match j.on(told) {
            Next::Show => {}
            Next::Address => self.ask_address(),
            Next::Unaddress => self.unaddress(),
            Next::Forget => {
                if let Some(r) = &self.running
                    && let Err(e) = r.control.order("REMOVE_NETWORK all")
                {
                    self.lost(e);
                }
            }
        }
    }

    /// Ask the network for an address, on a thread of its own.
    fn ask_address(&mut self) {
        let interface = self.cfg.interface.clone();
        let back = self.back.clone();
        let Some(r) = &mut self.running else { return };
        r.retry_at = None;
        if let Some((_, stop)) = r.asking.take() {
            stop.store(true, Ordering::Relaxed);
        }
        r.asked += 1;
        let n = r.asked;
        let stop = Arc::new(AtomicBool::new(false));
        r.asking = Some((n, stop.clone()));
        let mac = r.mac;
        let keeping = r.lease.as_ref().map(|h| (h.lease.clone(), h.phase));
        thread::spawn(move || {
            let answer = match keeping {
                None => dhcp::obtain(&interface, mac, &stop).map(Asked::Got),
                Some((lease, phase)) => dhcp::keep(&interface, mac, &lease, phase > 0, &stop).map(Asked::Kept),
            };
            back.tell(Inbox::Address(n, answer));
        });
    }

    fn addressed(&mut self, n: u64, answer: Result<Asked>) {
        let Some(r) = &mut self.running else { return };
        if r.asking.as_ref().map(|(m, _)| *m) != Some(n) {
            return;
        }
        r.asking = None;
        let lease = match answer {
            Ok(Asked::Got(lease)) | Ok(Asked::Kept(Kept::Kept(lease))) => lease,
            Ok(Asked::Kept(Kept::Unanswered)) => {
                if let Some(h) = &mut r.lease {
                    h.phase += 1;
                }
                return;
            }
            Ok(Asked::Kept(Kept::Refused)) => {
                self.unaddress();
                return self.ask_address();
            }
            Err(e) if e.is::<skyward_net::Stopped>() => return,
            Err(e) => {
                let _ = skyward_sys::kmsg(&format!("no address: {e:#}"));
                r.retry_at = Some(Instant::now() + RETRY);
                if let Some(j) = &mut self.state.joined {
                    j.stage = Stage::NoAddress(format!("{e:#}"));
                }
                return;
            }
        };
        let same = r.lease.as_ref().is_some_and(|h| (h.lease.address, h.lease.mask, h.lease.router) == (lease.address, lease.mask, lease.router));
        if !same
            && let Err(e) = iface::configure(&self.cfg.interface, lease.address, lease.mask, lease.router)
        {
            let _ = skyward_sys::kmsg(&format!("{e:#}"));
            r.retry_at = Some(Instant::now() + RETRY);
            if let Some(j) = &mut self.state.joined {
                j.stage = Stage::NoAddress(format!("{e:#}"));
            }
            return;
        }
        let _ = skyward_sys::kmsg(&format!(
            "{} has {} ({}) through {:?} for {} s, kept again after {} s",
            self.cfg.interface, lease.address, lease.mask, lease.router, lease.seconds, lease.renew
        ));
        *self.servers.lock().expect("the DNS servers are not held by a thread that panicked") = lease.dns.clone();
        let mut link = Link::new(&lease, r.mac);
        // An address kept keeps how far its network was found to reach; a
        // new one has it looked at.
        link.reach = match &self.state.joined {
            Some(Joined { stage: Stage::Connected(had), .. }) if same => had.reach.clone(),
            _ => None,
        };
        let look = link.reach.is_none().then(|| lease.dns.clone());
        r.lease = Some(Held { lease, at: Instant::now(), phase: 0 });
        if let Some(j) = &mut self.state.joined {
            j.stage = Stage::Connected(link);
        }
        write_resolv(&r.lease.as_ref().map(|held| held.lease.dns.clone()).unwrap_or_default());
        if let Some(dns) = look {
            self.probe(dns.clone());
            keep_alive(self.cfg.probe.clone(), dns);
        }
        self.poll_link();
    }

    /// Look how far the network joined reaches, on a thread of its own, the
    /// page's name looked up in the network's own DNS servers, `dns`.
    fn probe(&mut self, dns: Vec<Ipv4Addr>) {
        let (page, back) = (self.cfg.probe.clone(), self.back.clone());
        let Some(r) = &mut self.running else { return };
        r.probed += 1;
        let n = r.probed;
        thread::spawn(move || {
            let reach = reach::probe(&page, &dns);
            back.tell(Inbox::Reach(n, reach));
        });
    }

    fn reached(&mut self, n: u64, reach: Reach) {
        if self.running.as_ref().is_none_or(|r| r.probed != n) {
            return;
        }
        let Some(Joined { stage: Stage::Connected(link), .. }) = &mut self.state.joined else { return };
        let _ = skyward_sys::kmsg(&format!("the network on {} {reach}", self.cfg.interface));
        link.reach = Some(reach);
    }

    /// Give the address back, and stop asking for one or keeping it.
    fn unaddress(&mut self) {
        let Some(r) = &mut self.running else { return };
        r.retry_at = None;
        if let Some((_, stop)) = r.asking.take() {
            stop.store(true, Ordering::Relaxed);
        }
        if r.lease.take().is_some()
            && let Err(e) = iface::unconfigure(&self.cfg.interface)
        {
            let _ = skyward_sys::kmsg(&format!("{e:#}"));
        }
        // Nothing goes over a network left, and counting starts afresh on
        // the next one.
        if r.counted.take().is_some() {
            self.tell.tell(Report::Traffic(Traffic::default()));
        }
        self.servers.lock().expect("the DNS servers are not held by a thread that panicked").clear();
    }

    fn timers(&mut self) {
        let now = Instant::now();
        let Some(r) = &mut self.running else { return };
        if r.retry_at.is_some_and(|at| now >= at) {
            r.retry_at = None;
            return self.ask_address();
        }
        if r.asking.is_none()
            && let Some(h) = &r.lease
            && now >= h.due()
        {
            if h.phase >= 2 {
                // The lease ran out with no server to keep it.
                self.unaddress();
                if let Some(j) = &mut self.state.joined {
                    j.stage = Stage::Addressing;
                }
            }
            return self.ask_address();
        }
        if now >= r.polled_at + POLL {
            self.poll_link();
        }
        if let Some(r) = &self.running
            && r.lease.is_some()
            && r.counted.is_none_or(|c| now >= c.at + TRAFFIC)
        {
            self.count(now);
        }
    }

    /// Read what has gone over the interface, and tell how fast it went
    /// since the last reading.
    fn count(&mut self, now: Instant) {
        let stats = Path::new("/sys/class/net").join(&self.cfg.interface).join("statistics");
        let read = |name: &str| -> Result<u64> {
            let path = stats.join(name);
            let text = fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
            text.trim().parse().with_context(|| format!("{} holds no count", path.display()))
        };
        let (rx, tx) = match (read("rx_bytes"), read("tx_bytes")) {
            (Ok(rx), Ok(tx)) => (rx, tx),
            (Err(e), _) | (_, Err(e)) => return self.lost(e),
        };
        let Some(r) = &mut self.running else { return };
        if let Some(c) = r.counted {
            let secs = now.duration_since(c.at).as_secs_f64();
            let rate = |total: u64, before: u64| (total.saturating_sub(before) as f64 / secs).round() as u64;
            self.tell.tell(Report::Traffic(Traffic { rx: rate(rx, c.rx), tx: rate(tx, c.tx) }));
        }
        r.counted = Some(Counted { at: now, rx, tx });
    }

    /// Read the network joined for its signal and speed.
    fn poll_link(&mut self) {
        let Some(r) = &mut self.running else { return };
        r.polled_at = Instant::now();
        let Some(Joined { stage: Stage::Connected(link), .. }) = &mut self.state.joined else { return };
        // A plain STATUS has Android's supplicant tell every listener again
        // that the network is connected; this one leaves that out.
        let (signal, status) = match (r.control.ask("SIGNAL_POLL"), r.control.ask("STATUS-NO_EVENTS")) {
            (Ok(signal), Ok(status)) => (signal, status),
            (Err(e), _) | (_, Err(e)) => return self.lost(e),
        };
        let fields = supplicant::fields(&signal);
        if let Some(rssi) = fields.get("RSSI").and_then(|v| v.parse().ok()) {
            link.signal = rssi;
        }
        if let Some(f) = fields.get("FREQUENCY").and_then(|v| v.parse().ok()) {
            link.frequency = f;
        }
        link.speed = fields.get("LINKSPEED").and_then(|v| v.parse().ok());
        link.generation = supplicant::fields(&status).get("wifi_generation").and_then(|v| v.parse().ok());
    }
}

/// Load the driver where its interface is not there yet, and bring the
/// interface up.

/// 硬复位不经过 panic，什么都留不下。让可能存在的 oops 直接升级成 panic：
/// 只有真出了 oops 才会触发，平时没有任何影响；一旦触发，ramoops 也许能落一份。
fn arm_the_kernel() {
    // oops 仍然升级成 panic（配合 cmdline 的 always_kmsg_dump 落进 ramoops）；
    // 但 RCU stall 先不要直接 panic——让它把整机各 CPU 的栈打出来，我们才有得看。
    let settings = [
        ("/proc/sys/kernel/panic_on_oops", "1"),
        ("/proc/sys/kernel/panic", "20"),
        ("/proc/sys/kernel/panic_on_rcu_stall", "0"),
        ("/proc/sys/kernel/hung_task_all_cpu_backtrace", "1"),
        ("/proc/sys/kernel/hung_task_timeout_secs", "60"),
        ("/sys/module/rcupdate/parameters/rcu_cpu_stall_timeout", "10"),
    ];
    for (path, value) in settings {
        match fs::write(path, value) {
            Ok(()) => {
                let _ = skyward_sys::kmsg(&format!("wlan: {path} set to {value}"));
            }
            Err(e) => {
                let _ = skyward_sys::kmsg(&format!("wlan: cannot set {path}: {e}"));
            }
        }
    }
}

/// cnss2 一直在等 DMS QMI（那句 `DMS QMI connection not established`），
/// 而 DMS 由 /vendor/bin/cnss-daemon 提供，恢复里从来没起过它。
fn start_cnss_daemon() {
    let at = Path::new("/vendor/bin/cnss-daemon");
    if !at.exists() {
        let _ = skyward_sys::kmsg("wlan: /vendor/bin/cnss-daemon is not there");
        return;
    }
    let log = Path::new("/sdcard/Skyward/wlan-diag");
    let _ = fs::create_dir_all(log);
    let held = fs::File::create(log.join("cnss-daemon.log")).ok();
    let mut cmd = std::process::Command::new(at);
    cmd.env("LD_LIBRARY_PATH", "/vendor/lib64:/system/lib64")
        .current_dir("/")
        .stdin(Stdio::null());
    if let Some(file) = held.and_then(|f| f.try_clone().ok().map(|c| (f, c))) {
        cmd.stdout(Stdio::from(file.0)).stderr(Stdio::from(file.1));
    } else {
        cmd.stdout(Stdio::null()).stderr(Stdio::null());
    }
    match cmd.spawn() {
        Ok(child) => {
            let _ = skyward_sys::kmsg(&format!("wlan: cnss-daemon started as pid {}", child.id()));
        }
        Err(e) => {
            let _ = skyward_sys::kmsg(&format!("wlan: cannot start cnss-daemon: {e}"));
        }
    }
}

fn bring_up(cfg: &Config, modules: &[PathBuf]) -> Result<()> {
    mirror_kernel_log();
    arm_the_kernel();
    let net = Path::new("/sys/class/net").join(&cfg.interface);
    if !net.exists() {
        for file in &cfg.firmware {
            place(file)?;
        }
        start_cnss_daemon();
        for path in skyward_modules::load(modules, &cfg.modules)? {
            skyward_sys::kmsg(&format!("loaded {}", path.display()))?;
        }
        if cfg.unblock {
        unblock_radio();
    }
    for w in &cfg.writes {
            fs::write(&w.path, &w.value).with_context(|| format!("write {} to {}", w.value, w.path))?;
        }
        let start = Instant::now();
        let until = start + INTERFACE_WAIT;
        let mut next = 5u64;
        while !net.exists() {
            ensure!(Instant::now() < until, "the driver made no {} in {} seconds", cfg.interface, INTERFACE_WAIT.as_secs());
            let waited = start.elapsed().as_secs();
            if waited >= next {
                let _ = skyward_sys::kmsg(&format!("still waiting for {}: {} of {} seconds", cfg.interface, waited, INTERFACE_WAIT.as_secs()));
                next += 5;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    iface::set_up(&cfg.interface, true)
}

/// Put a file the driver asks for where the firmware loader looks.
fn place(file: &Firmware) -> Result<()> {
    let to = Path::new(FIRMWARE).join(&file.name);
    let dir = to.parent().expect("a firmware file is in a folder");
    fs::create_dir_all(dir).with_context(|| format!("make {}", dir.display()))?;
    let bytes = match &file.partition {
        None => fs::read(&file.path).with_context(|| format!("read {}", file.path))?,
        Some(partition) => {
            let at = Path::new(PARTITIONS).join(partition);
            fs::create_dir_all(&at).with_context(|| format!("make {}", at.display()))?;
            skyward_block::mount::mount_ro(&Path::new(skyward_block::BY_NAME).join(partition), &at)?;
            let read = fs::read(at.join(&file.path)).with_context(|| format!("read {} on {partition}", file.path));
            skyward_block::mount::umount(&at)?;
            read?
        }
    };
    fs::write(&to, bytes).with_context(|| format!("write {}", to.display()))
}

/// Start the supplicant on the interface, and wait for its control socket.
/// Tell the last lines of the supplicant's own log into the kernel log, so a
/// copy of kernel.log carries why it ended without opening a file by hand.
/// 把网络给的 DNS 写进 /etc/resolv.conf：终端里手敲命令下载时用得上。
fn write_resolv(dns: &[Ipv4Addr]) {
    if dns.is_empty() {
        return;
    }
    let text: String = dns.iter().map(|a| format!("nameserver {a}\n")).collect();
    match fs::write("/etc/resolv.conf", &text) {
        Ok(()) => {
            let _ = skyward_sys::kmsg(&format!("wlan: /etc/resolv.conf set to {}", text.replace('\n', " ")));
        }
        Err(e) => {
            let _ = skyward_sys::kmsg(&format!("wlan: cannot write /etc/resolv.conf: {e}"));
        }
    }
}

/// 每隔 20 秒探一次网；只做一次，之后自己转圈。
fn keep_alive(page: String, dns: Vec<Ipv4Addr>) {
    if KEEPING.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || {
        loop {
            let reach = reach::probe(&page, &dns);
            let _ = skyward_sys::kmsg(&format!("wlan: keepalive: {reach}"));
            thread::sleep(Duration::from_secs(20));
        }
    });
}

fn tell_log(path: &Path) {
    // 原样留一份在盘上，重启后还能逐行看，不受内核日志那几百行的窗口限制。
    for at in MIRROR {
        let dir = Path::new(at);
        if fs::create_dir_all(dir).is_ok() {
            let _ = fs::copy(path, dir.join("wpa_supplicant.log"));
        }
    }
    match fs::read_to_string(path) {
        Ok(text) => {
            let lines: Vec<&str> = text.lines().collect();
            let from = lines.len().saturating_sub(40);
            for line in &lines[from..] {
                let _ = skyward_sys::kmsg(&format!("wpa_supplicant: {line}"));
            }
            let _ = skyward_sys::kmsg(&format!("wpa_supplicant: {} lines in {}", lines.len(), path.display()));
        }
        Err(e) => {
            let _ = skyward_sys::kmsg(&format!("wpa_supplicant: read {}: {e}", path.display()));
        }
    }
}

/// 真系统库在恢复里的位置；有它才说明是这台的布局。
#[allow(dead_code)]
const REAL_LIB: &str = "/mnt/system/system/lib64";

/// 真系统里那份 libbinder：libbinder_ndk 要的 setTransactionCodeMap 只有它有。
/// 先按整条路径装进来，厂商那几个 hidl/ndk 库按 soname 就会复用它，不会再多出一份。
#[allow(dead_code)]
const REAL_BINDER: &str = "/mnt/system/system/lib64/libbinder.so";

/// 恢复里的链接器配置：写出来，厂商的 supplicant 就按 system/vendor 两个
/// 命名空间各取各的库，和真系统一样。
#[allow(dead_code)]
const LINKER_CONFIG: &str = "/linkerconfig/ld.config.txt";

/// 一份最小的命名空间配置：厂商二进制落在 vendor 命名空间（/vendor/lib64、
/// /odm/lib64），系统库通过 links 从真系统那份取；libc 这类还在 APEX 里的，
/// 退回 ramdisk 自己那层。
#[allow(dead_code)]
const LINKER_CONFIG_TEXT: &str = "\
dir.system = /system/bin\n\
dir.system = /mnt/system/system/bin\n\
dir.vendor = /vendor/bin\n\
dir.vendor = /odm/bin\n\
\n\
[system]\n\
namespace.default.isolated = true\n\
namespace.default.visible = true\n\
namespace.default.search.paths = /mnt/system/system/${LIB}\n\
namespace.default.search.paths += /system/${LIB}\n\
namespace.default.permitted.paths = /mnt/system/system\n\
namespace.default.permitted.paths += /system\n\
\n\
[vendor]\n\
namespace.default.isolated = true\n\
namespace.default.visible = true\n\
namespace.default.search.paths = /vendor/${LIB}\n\
namespace.default.search.paths += /odm/${LIB}\n\
namespace.default.permitted.paths = /vendor\n\
namespace.default.permitted.paths += /odm\n\
namespace.default.links = system\n\
namespace.system.isolated = true\n\
namespace.system.visible = true\n\
namespace.system.search.paths = /mnt/system/system/${LIB}\n\
namespace.system.search.paths += /system/${LIB}\n\
namespace.system.permitted.paths = /mnt/system/system\n\
namespace.system.permitted.paths += /system\n";

/// 把上面那份写进 /linkerconfig/ld.config.txt；写成了就返回 true。
/// 只在这台的挂载布局下写：别的机型没有 /mnt/system/system，就不会被动到。
#[allow(dead_code)]
fn write_linker_config() -> bool {
    if !Path::new(REAL_LIB).is_dir() {
        return false;
    }
    let path = Path::new(LINKER_CONFIG);
    let Some(dir) = path.parent() else { return false };
    if fs::create_dir_all(dir).is_err() {
        return false;
    }
    fs::write(path, LINKER_CONFIG_TEXT).is_ok()
}

/// 起完 supplicant 就把配置撤掉：它只在 exec 时读一次，留在那儿会让
/// 之后起的每一个进程（HIDL 服务、安装脚本…）都套上命名空间。
#[allow(dead_code)]
fn drop_linker_config() {
    let _ = fs::remove_file(LINKER_CONFIG);
}

fn start_supplicant(cfg: &Config, libraries: Option<&Path>) -> Result<Child> {
    let home = Path::new(HOME);
    let sockets = home.join("sockets");
    fs::create_dir_all(&sockets).with_context(|| format!("make {}", sockets.display()))?;
    fs::set_permissions(home, fs::Permissions::from_mode(0o755)).with_context(|| format!("open {} to the supplicant", home.display()))?;
    std::os::unix::fs::chown(&sockets, Some(WIFI), Some(WIFI)).with_context(|| format!("give {} to the supplicant", sockets.display()))?;
    fs::set_permissions(&sockets, fs::Permissions::from_mode(0o770)).with_context(|| format!("keep {} to the supplicant", sockets.display()))?;
    let settings = home.join("wpa_supplicant.conf");
    let text = format!("ctrl_interface={}\nupdate_config=0\np2p_disabled=1\npmf=1\nsae_pwe=2\n", sockets.display());
    fs::write(&settings, text).with_context(|| format!("write {}", settings.display()))?;
    fs::set_permissions(&settings, fs::Permissions::from_mode(0o644)).with_context(|| format!("open {} to the supplicant", settings.display()))?;
    let socket = sockets.join(&cfg.interface);
    // One an earlier run left would answer nothing.
    if socket.exists() {
        fs::remove_file(&socket).with_context(|| format!("take away {}", socket.display()))?;
    }
    let mut path: Vec<String> = cfg.libraries.clone();
    if let Some(dir) = libraries {
        path.push(dir.to_string_lossy().into_owned());
    }
    for at in ["/mnt/system", "/mnt/system/lib64", "/mnt/system/system", "/mnt/system/system/lib64"] {
        let said = match fs::read_dir(at) {
            Ok(dir) => {
                let names: Vec<String> = dir
                    .filter_map(|e| e.ok())
                    .take(12)
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .collect();
                format!("{at} holds {names:?}")
            }
            Err(e) => format!("{at}: {e}"),
        };
        let _ = skyward_sys::kmsg(&format!("supplicant: {said}"));
    }
    let env_path = path.join(":");
    // 恢复里没有 /linkerconfig/ld.config.txt，所以所有库挤在一个命名空间里，
    // 新版系统库和厂商库会互相要求对方没有的符号。自己写一份最小的：厂商二进制
    // 走 vendor 命名空间（libbinder 只此一份），系统库经 links 从真系统那份取。
    let mut cmd = Process::new(&cfg.supplicant);
    cmd.args(["-i", &cfg.interface, "-D", "nl80211", "-c"])
        .arg(&settings)
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?))
        .stderr(Stdio::from(log_file(&home.join("wpa_supplicant.log"))?));
    cmd.env("LD_LIBRARY_PATH", &env_path);
    let _ = skyward_sys::kmsg(&format!("supplicant: LD_LIBRARY_PATH={env_path}"));
    for probe in ["libapexsupport.so", "libbinder.so", "libbinder_ndk.so", "libc++.so", "liblog.so", "libc.so", "libbase.so"] {
        let found = path.iter().map(|dir| format!("{dir}/{probe}")).find(|at| Path::new(at).exists());
        let _ = skyward_sys::kmsg(&format!("supplicant: {probe} at {found:?}"));
    }
    let mut child = cmd.spawn().with_context(|| format!("start {}", cfg.supplicant))?;
    let until = Instant::now() + SOCKET_WAIT;
    while !socket.exists() {
        if let Some(status) = child.try_wait().context("look at wpa_supplicant")? {
            tell_log(&home.join("wpa_supplicant.log"));
            bail!("wpa_supplicant ended as it started: {status}");
        }
        if Instant::now() >= until {
            end(&mut child, None);
            tell_log(&home.join("wpa_supplicant.log"));
            bail!("wpa_supplicant opened no control socket in {} seconds", SOCKET_WAIT.as_secs());
        }
        thread::sleep(Duration::from_millis(100));
    }
    Ok(child)
}

/// End the supplicant: asked to, where it can be, and killed after a while.
fn end(child: &mut Child, control: Option<&Control>) {
    if let Some(c) = control {
        let _ = c.ask("TERMINATE");
        let until = Instant::now() + END_WAIT;
        while Instant::now() < until {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

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

/// 打开（或创建）supplicant 的日志文件：它退出时留下的输出是判断原因的唯一线索。
fn log_file(path: &std::path::Path) -> Result<fs::File> {
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("open {}", path.display()))
}
