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
| A9 | Clients | per-device speed limit, block/unblock | P1 | nft, tc |
| A10 | Data usage | daily/monthly stats, monthly cap and reset day, warn/block | P1 | own collector (no vnstat) |
| A11 | LAN/DHCP | gateway IP/netmask, DHCP range/lease, static leases | P1 | dnsmasq, NM bridge |
| A12 | Firewall | port forwarding, port filter, URL/IP allow/deny lists, DNS switch | P1 | nft, dnsmasq |
| A13 | SMS/MMS | inbox/outbox, send/reply/delete, MMS receive (text + pictures) ✅; contacts | P1 ✅ (contacts: P2) | mmcli --messaging, own MMS client |
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
