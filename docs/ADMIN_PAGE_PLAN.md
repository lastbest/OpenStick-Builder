# Admin page plan

Goal: everything the stock (vendor) admin page of these dongles offered, plus the
features most home routers have, on top of this Debian image — without eating the
stick's ~370 MB of usable RAM. Code: [`admin/`](../admin/).

The stock page was an Android app (`/system/priv-app/ufilauncherzx.apk` on the UF902
stock firmware: a Vue + ElementUI SPA with `/api/system/*` and `/api/wifi/*`).
Its menus were used as the checklist below; the intercom / agent / announcement screens
in that app are leftovers of the vendor's template and are not dongle features.

Priorities: **P0** done in v0.1 · **P1** next · **P2** later · **P3** needs hardware validation.

## A. Stock admin page features
| # | Area | Feature | Prio | Implementation |
|---|---|---|---|---|
| A1 | Login | admin login/logout, change password | P0 ✅ | built-in |
| A2 | Dashboard | signal (RSRP/RSRQ/SINR), operator, RAT, state, WAN IP, live speed, usage, clients, SIM, uptime/CPU/RAM | P0 ✅ | mmcli, /proc, /sys |
| A3 | Quick setup | WiFi name/password on one screen | P0 ✅ | hostapd.conf |
| A4 | Internet | data on/off, auto/manual connect, no data while roaming | P0 ✅ | nmcli (`lte`) |
| A5 | Internet | APN profiles (APN/user/password/auth/IP type), current APN | P0 ✅ | nmcli gsm.* |
| A6 | Internet | network mode (auto / LTE only), manual operator selection, MTU | P1 | mmcli --set-allowed-modes, --3gpp-scan |
| A7 | WiFi | SSID, password, security, hidden SSID, channel, max clients, AP isolation | P0 ✅ | hostapd.conf |
| A8 | Clients | list (name/IP/MAC/WiFi·USB/time) | P0 ✅ | dnsmasq leases, hostapd_cli, ip neigh |
| A9 | Clients | block/unblock (P1), per-device speed limit (approximate only, see D) | P1 | nft |
| A10 | Data usage | daily/monthly stats, monthly cap and reset day, warn/block | P1 | own collector (no vnstat) |
| A11 | LAN/DHCP | gateway IP/netmask, DHCP range/lease, static leases | P1 | dnsmasq, NM bridge |
| A12 | Firewall | port forwarding, port filter, URL/IP allow/deny lists, DNS switch | P1 | nft, dnsmasq |
| A13 | SMS/MMS | inbox/outbox, send/reply/delete, MMS receive + send (text, pictures, files) ✅; contacts | P1 ✅ (contacts: P2) | mmcli --messaging, own MMS client |
| A14 | SIM | PIN enable/change/disable, PIN/PUK retries | P1 | mmcli --sim |
| A15 | USSD | request/response | P2 | mmcli --3gpp-ussd |
| A16 | Device | info ✅, reboot ✅, factory reset of settings, LEDs off, time/timezone, logs, language | P0/P1 | sysfs leds, timedatectl, journalctl |
| A17 | Upgrade | admin page self-update, package security updates | P2 | upload / apt |

Deliberately **not** implemented: IMEI change (illegal in many countries), carrier SIM
locks (SP/PC/PU/MEP), battery/power-saving (no battery).

## B. Common router features
| # | Feature | Prio | Note |
|---|---|---|---|
| B1 | DNS: ad blocking on/off, upstream choice | P1 | uses the existing dnsproxy (AdGuard DoH/DoQ) |
| B2 | WiFi MAC allow/deny list | P1 | hostapd accept/deny |
| B3 | Diagnostics: ping/traceroute/nslookup, speed test | P1 | |
| B4 | Settings backup/restore | P1 | |
| B5 | System log, service status, WiFi/hostapd restart | P1 | journalctl |
| B6 | DMZ | P2 | nft |
| B7 | UPnP | P2 | needs miniupnpd |
| B8 | DDNS | P2 | carrier CGNAT (10.x) makes it useless on most SIMs — explain in UI |
| B9 | WireGuard server/client | P2 | server limited by CGNAT |
| B10 | Parental control / internet schedule per device | P2 | nft + timers |
| B11 | WiFi schedule, scheduled reboot | P2 | systemd timers |
| B12 | LTE cell info, band lock | P2 | mmcli --set-current-bands |
| B13 | IPv6 settings | P2 | |
| B14 | USB mode (NCM/RNDIS/ECM) | P2 | /etc/msm8916-usb-gadget.conf |
| B15 | SSH on/off, keys | P2 | |
| B16 | Guest WiFi (2nd SSID, isolated) | P3 | single wcn36xx radio — multi-BSS untested |
| B17 | QoS | P3 | |

Not planned: WiFi repeater (AP + STA on wcn36xx crashes the WCNSS firmware, taking the
modem with it on some sticks), TTL spoofing.

## C. APN screen behaviour
- Default **auto**: NetworkManager `gsm.auto-config=yes` picks the APN from
  mobile-broadband-provider-info by the SIM's MCC/MNC (e.g. a KT MVNO SIM, 45008 →
  `alwayson.ktfwing.com`, IPv4v6).
- Manual profiles can be added and the user can always go back to auto.
- Saving reconnects LTE and waits for an IP; if that fails the previous settings are
  restored automatically, because LTE is the only uplink.

## D. P1 design (checked on a JZ01-45-V33 stick, Oct 2026)
Device facts that shape the design: the pmOS kernel has **no tc qdiscs** (no htb/tbf/ifb),
so real per-client shaping is impossible — only nft `limit` policing (drops). nft
counters/limit/conntrack, br_netfilter and WireGuard are available. `traceroute`, `dig`,
miniupnpd and ddclient are not installed. Carrier IPs are CGNAT (10.x).

Shared building blocks first:
- **apply-with-confirm**: risky changes are reverted automatically unless confirmed in the
  UI within 60 s (LAN IP, DNS, MAC filter, firewall);
- **no self-lockout**: refuse block/filter rules that would cut off the requesting client;
- a dedicated nft table `inet osa` for everything the admin page adds.

| # | Feature | Design | Risk / limits | Effort | Decision |
|---|---|---|---|---|---|
| A10 | Data usage, monthly cap, warn/block | nft counters on wwan0 → daily rows in sqlite every 60 s; block = forward drop to wwan0 | a few % off the carrier's bill; includes the stick's own traffic | M | do first |
| A9a | Block/unblock client | nft drop by MAC | randomized MACs (iOS private address) escape after a MAC change | S | do |
| A9b | Per-client speed limit | nft `limit rate over` | no tc → coarse, bursty; label as approximate | M | later / approximate |
| A6 | Network mode (auto / LTE only), operator scan | `mmcli --set-allowed-modes`, read-only `--3gpp-scan` | scan takes 1–3 min and may drop data; LTE-only can lose service; manual registration pointless on domestic SIMs (skipped) | S–M | do |
| A11a | DHCP range/lease, static leases | dnsmasq `dhcp-range` / `dhcp-host` | low | S | do |
| A11b | Change LAN IP/netmask | NM bridge + dnsmasq + nft + admin bind + hosts, with auto-revert | lock-out risk, rarely needed | L | defer |
| A12a | Outbound IP/port filter, domain blocking | nft; dnsmasq `address=/domain/` (DNS already forced to dnsmasq) | clients using DoH bypass domain blocking | M | do |
| A12b | Port forwarding, DMZ | nft dnat | useless behind CGNAT; only for public-IP plans | M | defer, explain in UI |
| A14 | SIM PIN enable/change/disable | `mmcli -i --enable-pin/--change-pin/--disable-pin` | an enabled PIN locks the SIM on every boot (no data/SMS) → unlock screen + optional stored PIN for auto-unlock; refuse when 1 try is left | M | do, with guards |
| A16 | LEDs, timezone, logs, service status, WiFi restart | sysfs leds (+persist), timedatectl, journalctl, systemctl | low | S | do |
| A16b | Reset settings | restore managed configs to image defaults | must track the file list | M | with B4 |
| B1 | Ad blocking on/off, upstream DNS | rewrite dnsproxy args, restart, verify resolution, revert on failure | DNS outage → auto-revert | S–M | do |
| B2 | WiFi MAC allow/deny | hostapd `macaddr_acl` + files | allow-list missing the requester → refuse + revert | S | do |
| B3 | Diagnostics, speed test | ping; DNS lookup in Python; traceroute package to be added | speed test uses metered LTE data → explicit size + confirm | S | do |
| B4 | Settings backup/restore | tar of managed configs (password hash optional), validate before restore | bad restore → validation + revert | M | do |

Order: shared helpers → A10 → A9a → A16/B5 → B1 → B3, then A11a → A12a → B2 → A14 → B4/A16b.

**Status (Oct 2026): P1-a done and verified on the device** — A10, A9a, A16/B5, B1, B3. A6 dropped (not needed).
The apply-with-confirm helper is not needed by P1-a (DNS changes verify themselves) and moves to P1-b.
Deferred: A9b, A11b, A12b, manual operator selection, i18n.
