# UF902 / JZ01-45-V33 — notes, fixes and flashing troubleshooting

Notes from putting this builder's Debian image on a **UF902** "4G-UFI" dongle
(Qualcomm **MSM8916**, 512 MB RAM, 4 GB eMMC, board **JZ01-45-V33**, unfused).
End result: Debian 13 + kernel 6.12.1 running with LTE (KT, Korea), the WiFi AP,
USB networking (RNDIS + CDC-NCM for macOS) and NAT working across reboots.

The image built from the unmodified `debian` branch did **not** work on this board.
Everything that had to be fixed is listed in §3 and is part of this fork.

---

## 1. Lineage

```
kinsamanka/OpenStick-Builder          (upstream base)
├── LongQT-sea/OpenStick-Builder      (sibling fork, minimal: no tor/nftables/hostapd)
└── Mio-sha512/OpenStick-Builder
    └── dbrant/OpenStick-Builder      ← branch `debian` @ 4ef3ac5, base of this fork
```

`RELEASE=stable` means the rootfs is whatever Debian stable is at build time
(Debian 13 "trixie" in Oct 2026). Pin with `RELEASE=bookworm` if needed.
tor / hostapd / nftables / dnsproxy come from the dbrant/Mio-sha512 line.

---

## 2. Device facts

| | |
|---|---|
| SoC / HWID | MSM8916 / `0x007050e1` |
| PK_HASH | `0xcc3153a80293939b…` (unfused, so any signed-or-test-signed loader works) |
| Firehose loader that matched | `007050e10029001e_cc3153a80293939b_fhprg_peek.bin` (bkerler Loaders) |
| eMMC | 7,733,248 × 512 B sectors |
| Board / kernel DTB | `JZ01-45-V33` → `msm8916-jz01-45-v33.dtb` |
| Button | GPIO37, active-high |
| LEDs | red = GPIO8 (power), green = GPIO7 (wlan), blue = GPIO6 (wan) |
| EDL | hold the internal button while plugging USB → `05c6:9008` |

---

## 3. Problems found and what this fork changes

| # | Symptom | Root cause | Fix in this fork |
|---|---|---|---|
| 1 | After flashing, the stick sits in **lk2nd fastboot** forever; log: `Detected device: uz801 v3.0`, `ERROR: No recovery partition found`, `Could not do normal boot` | `build_hyp_aboot.sh` hard-coded `LK2ND_COMPATIBLE="yiming,uz801-v3"`. The uz801 node watches **GPIO23 active-low**, which reads as "held" on JZ01-45-V33, so every boot is treated as a recovery request | `BOARD=` build option (`scripts/board.sh`) + `patches/lk2nd-0001-add-jz01-45-v33.patch` adding an lk2nd node with **GPIO37 active-high/pull-down**. Same values were verified on the device by binary-patching the lk2nd DTB and re-signing with qtestsign |
| 2 | Linux boots (LED on, rootfs mounted) but **no USB gadget, no WiFi, no nf_tables** | The postmarketOS kernel `.apk` ships an **empty `modules.alias`** and a `modules.dep` **without dependencies** (pmOS regenerates them on install). Nothing autoloads | `debootstrap.sh` runs `depmod -a` inside the chroot (target kmod handles `.ko.zst`) and fails the build if `modules.alias` is still empty |
| 3 | Build aborts at kernel install (404) | `linux-postmarketos-qcom-msm8916-6.12.1-r2.apk` removed from the mirror | `KERNEL_APK` variable, default `6.12.1-r6` (current in APKINDEX, Oct 2026), https |
| 4 | WiFi/USB clients get an IP but **no internet** | `nftables.conf` used `redirect to :53` — the pmOS kernel has **no `CONFIG_NFT_REDIR`**, nft rejects the whole file, so masquerade is never loaded. Also `iif "br0"` fails at boot because NetworkManager creates br0 later | `dnat to 192.168.100.1:53` / `[dead:beef::1]:53` and `iifname "br0"` |
| 5 | `ip_forward = 0` after boot | Debian 13 no longer applies `/etc/sysctl.conf` | `configs/sysctl.d/99-openstick-forward.conf` |
| 6 | **WiFi AP dies** after a while and never comes back; kernel log `wcn36xx: ERROR Timeout! No SMD response`, WCNSS restarts, hostapd stopped | NetworkManager + wpa_supplicant run periodic background scans on `wlan0` while hostapd uses it as an AP → WCNSS firmware crash. `hostapd.service` has `BindsTo=wlan0`, so it stops and is not restarted when wlan0 re-appears | `wlan0` unmanaged in `99-custom.conf`, `wpa_supplicant` disabled, `hostapd.service.d/autostart.conf` (`WantedBy=sys-subsystem-net-devices-wlan0.device`) |
| 7 | Wrong devicetree for non-UZ801 boards | `extlinux.conf` always pointed at `yiming-uz801v3` | `BOARD=` also selects the kernel DTB |

Optional, not changed in defaults: macOS needs CDC-NCM — set `ENABLE_NCM=1` in
`/etc/msm8916-usb-gadget.conf` (RNDIS can stay on for Windows).

**Testing status (honest):** fixes 1, 2, 4, 5, 6, 7 were applied and verified on the
running device (including reboots). The build-script changes themselves have
**not been test-built yet** (no Linux build host at hand). The lk2nd patch was checked
to apply to the pinned submodule and to compile with `cpp`+`dtc` to the expected node.

---

## 4. Flashing: what worked and what did not

### 4.1 EDL writes time out on WSL / Windows
Sahara loader upload, Firehose reads and XML commands worked, but every
`program` (write) timed out (`USBTimeoutError` 110 / 10060) on both
**WSL2 + usbipd-win** and **native Windows + libusb/WinUSB (Zadig)**, with any loader,
`ZLPAwareHost=0`, larger timeouts, smaller bulk buffers, other pyusb versions.
On a **native macOS (Apple Silicon) host** with Homebrew libusb, writes worked at
once (~10–20 MB/s) and Firehose sessions could even be reused without replugging.
Use native Linux or macOS for flashing.

### 4.2 lk2nd needs the DB410c firmware set
With lk2nd (`aboot`) + qhypstub (`hyp`) but the **stock** `sbl1/rpm/tz`, the stick
did not enumerate on USB at all (the lk2nd dts also notes the stock firmware is
incompatible with qhypstub). Flashing the builder's `sbl1/rpm/tz` (DragonBoard 410c)
fixed it. Because `tz.mbn` (605 KB) is larger than the stock `tz` partition (512 KB),
the new partition table has to be written first.

### 4.3 EDL-only install (no fastboot step needed)
When lk2nd fastboot is not reachable, everything can be written over EDL:
1. back up the whole eMMC (`edl rl`), especially `modem modemst1 modemst2 fsg fsc sec persist`;
2. write a full-disk GPT (primary at sector 0, backup at the last 33 sectors) whose
   rootfs ends at the last usable LBA (the 34-sector `gpt_both0.bin` relies on lk2nd's
   fastboot to stretch the last partition, which EDL does not do);
3. `edl w` `sbl1 rpm tz hyp aboot`, the **raw** (un-sparsed) `boot`/`rootfs`, and the
   backed-up device-unique partitions by name;
4. read back and compare every write, then `edl reset`.

### 4.4 Misc
- Take the **latest** `modemst1/modemst2/persist` right before flashing — they change
  while the stock firmware runs; restoring an older copy is avoidable risk.
- A first look at a half-flashed stick can be misleading: our WSL attempt was believed
  to have erased `boot`, but reading the eMMC showed `boot` intact and `aboot` already
  replaced by lk2nd (that write had in fact completed). Always read and compare before
  "repairing".
- APN: with `gsm.auto-config=yes` NetworkManager picked the right APN for a KT MVNO SIM
  (MCC/MNC 45008 → `alwayson.ktfwing.com`) from mobile-broadband-provider-info.

---

## 5. Build for this board

```sh
git clone --recurse-submodules -b uf902-jz01-fixes https://github.com/<this-fork>/OpenStick-Builder
cd OpenStick-Builder
sudo BOARD=jz01-45-v33 ./build.sh
# files/: aboot.mbn hyp.mbn rpm.mbn sbl1.mbn tz.mbn boot.bin rootfs.bin gpt_both0.bin
```
