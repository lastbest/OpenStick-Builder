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

Roadmap: [`../docs/ADMIN_PAGE_PLAN.md`](../docs/ADMIN_PAGE_PLAN.md).

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
