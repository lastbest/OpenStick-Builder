# OpenStick admin page

A small web UI for this image, replacing the vendor admin page of the stock
firmware. Served on **http://192.168.100.1/** (LAN side only).

- Backend: Python 3 **standard library only** (`http.server`), ~15 MB RSS, systemd `MemoryMax=40M`.
- Frontend: plain HTML/JS/CSS, no framework, mobile-first. UI language: Korean (English is planned).
- Talks to the system through `mmcli`, `nmcli`, `hostapd.conf`/`hostapd_cli`, `ip`, sysfs —
  argument lists only (no shell), every input validated.

It is written for the layout this builder produces (NetworkManager connection `lte`,
`/etc/hostapd/hostapd.conf`, bridge `br0` = `192.168.100.1`, dnsmasq leases), which is
why it lives in this repository.

## Features (v0.1)
| Page | |
|---|---|
| Login | first visit sets the admin password; logout; change password |
| Dashboard | LTE signal (bars, RSRP/RSRQ/SINR), connection/operator/WAN IP/APN/SIM, live throughput, data used on the current bearer, clients, uptime/memory/load/temperature (3 s refresh) |
| WiFi | SSID, password, WPA2 or WPA2+WPA3, channel, max clients, hidden SSID, AP isolation |
| Internet | mobile data on/off, roaming, APN **auto/manual** (user/password/IP type) with **automatic rollback** if the new APN does not connect |
| Clients | name / IP / MAC / WiFi or USB / signal / connected time |
| Device | model, OS, kernel, IMEI, modem firmware, ICCID, IMSI; reboot |
| Messages | SMS inbox/outbox, send and reply, **MMS** (long texts, pictures) downloaded automatically, unread badge, bulk/age delete, retry; attachments capped at 500 MB / 300 MB free (oldest pruned, text kept) |
| Data usage | daily/period usage from nft counters on wwan0, monthly cap with reset day, warning, optional block of client internet when over cap |
| Clients | block/unblock a client's internet by MAC (refuses to block the requesting client) |
| Diagnostics | upstream DNS (AdGuard ad-blocking / Cloudflare / Google / carrier, verified + reverted on failure), ping, DNS lookup, traceroute, metered speed test |
| System | LEDs on/off (blue = LTE up), timezone, service status/restart, journal viewer |

Roadmap: [`../docs/ADMIN_PAGE_PLAN.md`](../docs/ADMIN_PAGE_PLAN.md).

## SMS / MMS (`sms.py`)
- **EPS UE mode.** These modems come up as `ps-2` (data-only attach), in which the
  network never delivers SMS. The collector switches to `csps-2` (data centric,
  combined attach → SMS over SGs) on start and whenever it reverts (e.g. after reboot).
- **Collector** (every 15 s): copies received SMS into `/var/lib/openstick-admin/messages.db`
  (deduplicated — the network often delivers the same SMS twice) and deletes them from the
  modem. Modem memory (`me`) is small and is wiped on reboot. Messages that piled up at the
  carrier while the stick was off arrive in a burst and are handled the same way.
- **MMS.** WAP-push notifications are parsed (OMA MMS / WSP) and the message is fetched with
  a plain HTTP GET from its Content-Location over the normal data connection (tested on KT:
  `d-mmsc.ktfwing.com:9083`, same APN as data). Text and images are stored; an
  `m-notifyresp-ind` is posted to the MMSC (best effort). No mmsd-tng needed.
  Korean carriers deliver long texts and pictures as MMS, so this matters there.
- **Sending:** SMS only; ModemManager splits long texts into concatenated SMS.
- Expired MMS (stick offline longer than the carrier keeps them) are shown as such.

## nftables
The admin page keeps its own table `inet osa` (wan_rx/wan_tx counters, `blocked` MAC set,
`capblock` set). It is re-created from `/var/lib/openstick-admin/settings.json` whenever it
is missing (e.g. after `systemctl restart nftables`, which flushes the ruleset).
The pmOS kernel has no tc qdiscs, so per-client bandwidth shaping is not offered.

## Install
- **New images:** included by default (`WITH_ADMIN=0 ./build.sh` to leave it out).
- **Existing device:** copy this directory to the stick and run `sh install.sh` as root.

## Develop without a device
```sh
python3 server.py --mock --listen 127.0.0.1:8080 --config ./dev-config.json
```
`mock.py` returns canned data (identifiers are fake). Saving the APN `fail.test`
exercises the rollback path. `--dev-autologin` (only together with `--mock`) skips
login for screenshots.

## Security
- Password: PBKDF2-SHA256 (200k iterations) in `/etc/openstick-admin/config.json` (0600).
- Session cookie `HttpOnly; SameSite=Strict`, 30 min idle timeout, `X-CSRF-Token` required on every POST.
- Increasing delay after 3 failed logins (max 5 min). CSP, `X-Frame-Options: DENY`, `nosniff`.
- Binds to `192.168.100.1` only, so it is not reachable from the LTE side.

## Notes from the device
- `wwan0` (BAM-DMUX) does not update its sysfs byte counters, so data usage comes from
  ModemManager bearer statistics and live throughput from the `br0` counters.
- The SIM often has no operator name; the network operator name is shown instead.
- Status: read paths verified on a JZ01-45-V33 stick; write paths (WiFi/APN changes)
  verified in mock mode only so far.
