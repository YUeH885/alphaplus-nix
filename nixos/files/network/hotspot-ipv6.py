#!/usr/bin/env python3
"""Configure IPv6 prefix sharing (RFC 7278 / NAT66) for NM shared hotspots."""

import fcntl
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

IP_BIN = "@ip@"
NFT_BIN = "@nft@"
NMCLI_BIN = "@nmcli@"
RADVD_BIN = "@radvd@"
SYSCTL_BIN = "@sysctl@"

STATE_DIR = Path("/run/alphaplus-hotspot")
LOCK_FILE = STATE_DIR / "lock"
STATE_FILE = STATE_DIR / "state.json"
RADVD_CONF = STATE_DIR / "radvd.conf"
RADVD_PID = STATE_DIR / "radvd.pid"

NFT_TABLE = "alphaplus_hotspot"
ROUTE_PROTO = "200"
ULA_ADDR = "fd42:6c6d:6738:1::1/64"
ULA_PREFIX = "fd42:6c6d:6738:1::/64"
ARPHRD_RAWIP = 519
DEPRECATE_SECONDS = 60

RELEVANT_ACTIONS = {
    "up",
    "down",
    "dhcp4-change",
    "dhcp6-change",
    "reapply",
    "vpn-up",
    "vpn-down",
}


def run(cmd, *, input_text=None, check=False):
    res = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and res.returncode != 0:
        if res.stderr:
            sys.stderr.write(res.stderr)
        res.check_returncode()
    return res


def sysctl_iface_key(iface):
    return iface.replace(".", "/")


def read_radvd_pid():
    try:
        pid = int(RADVD_PID.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def is_radvd_pid(pid):
    if not pid:
        return False
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == "radvd"
    except OSError:
        return False


def radvd_running():
    return is_radvd_pid(read_radvd_pid())


def stop_radvd():
    pid = read_radvd_pid()
    if is_radvd_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.05)
        if Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    RADVD_PID.unlink(missing_ok=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def clear_dynamic_v6(state):
    run([IP_BIN, "-6", "route", "flush", "proto", ROUTE_PROTO])

    if state.get("mode") == "ula" and state.get("shared_iface"):
        run([IP_BIN, "-6", "addr", "del", ULA_ADDR, "dev", state["shared_iface"]])

    if state.get("mode") == "rfc7278" and state.get("shared_iface"):
        for addr in state.get("proxy_addrs", []):
            run([IP_BIN, "-6", "neigh", "del", "proxy", addr, "dev", state["shared_iface"]])


def find_shared_iface():
    res = run([NMCLI_BIN, "-t", "-f", "DEVICE,UUID", "connection", "show", "--active"])
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if ":" not in line:
            continue
        dev, uuid = line.split(":", 1)
        if not dev or dev == "--":
            continue
        mres = run([NMCLI_BIN, "-g", "ipv4.method", "connection", "show", uuid])
        if mres.returncode == 0 and mres.stdout.strip() == "shared":
            return dev
    return None


def get_iface_v6_addrs(iface):
    res = run([IP_BIN, "-6", "-j", "addr", "show", "dev", iface])
    if res.returncode != 0:
        return [], None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return [], None
    if not data:
        return [], None

    globals_v6 = []
    link_local = None
    for info in data[0].get("addr_info", []):
        if info.get("family") != "inet6":
            continue
        scope = info.get("scope")
        if scope == "link" and link_local is None:
            link_local = info.get("local")
        elif scope == "global" and not info.get("deprecated", False):
            globals_v6.append((info.get("local"), int(info.get("prefixlen", 128))))
    return globals_v6, link_local


def find_ipv6_upstream(shared_iface):
    res = run([IP_BIN, "-6", "-j", "route", "show", "default"])
    if res.returncode != 0:
        return None
    try:
        routes = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None

    candidates = [
        r for r in routes
        if r.get("dev") not in (None, "", "lo", shared_iface)
        and str(r.get("protocol")) != ROUTE_PROTO
    ]
    candidates.sort(key=lambda r: int(r.get("metric", 0)))

    for route in candidates:
        up_iface = route["dev"]
        addrs, _ = get_iface_v6_addrs(up_iface)
        if not addrs:
            continue

        type_path = Path(f"/sys/class/net/{up_iface}/type")
        try:
            link_type = int(type_path.read_text().strip())
        except (OSError, ValueError):
            link_type = 0

        prefix64 = next(
            (str(ipaddress.IPv6Network(f"{addr}/{plen}", strict=False))
             for addr, plen in addrs if plen == 64),
            None,
        )
        return {
            "iface": up_iface,
            "gateway": route.get("gateway"),
            "addrs": [addr for addr, _ in addrs],
            "link_type": link_type,
            "prefix64": prefix64,
        }
    return None


def apply_nft(nat66_up_iface=None):
    if not nat66_up_iface:
        run([NFT_BIN, "delete", "table", "inet", NFT_TABLE])
        return

    lines = [
        f"add table inet {NFT_TABLE}",
        f"flush table inet {NFT_TABLE}",
        f"add chain inet {NFT_TABLE} nat_postrouting {{ type nat hook postrouting priority srcnat; policy accept; }}",
        f'add rule inet {NFT_TABLE} nat_postrouting oifname "{nat66_up_iface}" ip6 saddr {ULA_PREFIX} masquerade',
    ]
    run([NFT_BIN, "-f", "-"], input_text="\n".join(lines) + "\n", check=True)


def build_radvd_conf(iface, prefix, link_local, deprecate_prefix=None):
    blocks = [
        "    AdvSendAdvert on;",
        "    IgnoreIfMissing on;",
        "    MinRtrAdvInterval 3;",
        "    MaxRtrAdvInterval 10;",
        "    AdvDefaultLifetime 1800;",
        f"    prefix {prefix} {{",
        "        AdvOnLink on;",
        "        AdvAutonomous on;",
        "        AdvValidLifetime 3600;",
        "        AdvPreferredLifetime 1800;",
        "    };",
    ]
    if deprecate_prefix and deprecate_prefix != prefix:
        blocks.extend([
            f"    prefix {deprecate_prefix} {{",
            "        AdvOnLink on;",
            "        AdvAutonomous on;",
            "        AdvValidLifetime 0;",
            "        AdvPreferredLifetime 0;",
            "    };",
        ])
    if link_local:
        blocks.extend([
            f"    RDNSS {link_local} {{",
            "        AdvRDNSSLifetime 1800;",
            "    };",
        ])

    return f"interface {iface} {{\n" + "\n".join(blocks) + "\n};\n"


def kernel_net_config(state):
    return (
        state.get("mode"),
        state.get("shared_iface"),
        state.get("prefix"),
        state.get("gateway"),
        state.get("up_iface"),
        sorted(state.get("proxy_addrs", [])),
    )


def apply_kernel_net(state):
    if state["mode"] == "rfc7278":
        # RFC 7278: share the cellular /64 directly while keeping the gateway on-link via /128.
        if state["gateway"]:
            run([
                IP_BIN, "-6", "route", "replace",
                f"{state['gateway']}/128", "dev", state["up_iface"],
                "proto", ROUTE_PROTO, "metric", "50",
            ], check=True)
        run([
            IP_BIN, "-6", "route", "replace",
            state["prefix"], "dev", state["shared_iface"],
            "proto", ROUTE_PROTO, "metric", "100",
        ], check=True)
        for addr in state["proxy_addrs"]:
            run([IP_BIN, "-6", "neigh", "replace", "proxy", addr, "dev", state["shared_iface"]], check=True)
        apply_nft(nat66_up_iface=None)
    elif state["mode"] == "ula":
        run([IP_BIN, "-6", "addr", "replace", ULA_ADDR, "dev", state["shared_iface"], "nodad"], check=True)
        apply_nft(nat66_up_iface=state["up_iface"])
    else:
        apply_nft(nat66_up_iface=None)


def compute_deprecation(prev_state, next_prefix, now):
    prev_prefix = prev_state.get("prefix")
    if prev_prefix and prev_prefix != next_prefix:
        return prev_prefix, now + DEPRECATE_SECONDS

    prev_dep = prev_state.get("deprecated_prefix")
    prev_until = float(prev_state.get("deprecated_until", 0) or 0)
    if prev_dep and prev_until > now and prev_dep != next_prefix:
        return prev_dep, prev_until

    return None, 0.0


def reconcile():
    prev_state = load_state()
    shared_iface = find_shared_iface()

    if not shared_iface:
        stop_radvd()
        clear_dynamic_v6(prev_state)
        apply_nft(nat66_up_iface=None)
        sysctl_args = []
        if prev_state.get("shared_iface"):
            sysctl_args.append(f"net.ipv6.conf.{sysctl_iface_key(prev_state['shared_iface'])}.proxy_ndp=0")
        if prev_state.get("enabled_forwarding"):
            sysctl_args.extend(["net.ipv4.ip_forward=0", "net.ipv6.conf.all.forwarding=0"])
        if sysctl_args:
            run([SYSCTL_BIN, "-w", *sysctl_args])
        STATE_FILE.unlink(missing_ok=True)
        RADVD_CONF.unlink(missing_ok=True)
        return

    if prev_state.get("shared_iface") and prev_state["shared_iface"] != shared_iface:
        run([SYSCTL_BIN, "-w", f"net.ipv6.conf.{sysctl_iface_key(prev_state['shared_iface'])}.proxy_ndp=0"])

    run([
        SYSCTL_BIN,
        "-w",
        "net.ipv4.ip_forward=1",
        "net.ipv6.conf.all.forwarding=1",
        f"net.ipv6.conf.{sysctl_iface_key(shared_iface)}.proxy_ndp=1",
    ])

    ula_iface = ipaddress.IPv6Interface(ULA_ADDR)
    shared_globals, shared_ll = get_iface_v6_addrs(shared_iface)
    external_pd = any(
        ipaddress.IPv6Interface(f"{addr}/{plen}") != ula_iface
        for addr, plen in shared_globals
    )
    upstream = None if external_pd else find_ipv6_upstream(shared_iface)

    if external_pd or not upstream:
        new_state = {"mode": "none", "enabled_forwarding": True, "shared_iface": shared_iface}
        if prev_state.get("mode") not in (None, "none") or radvd_running():
            stop_radvd()
            clear_dynamic_v6(prev_state)
            apply_nft(nat66_up_iface=None)
            RADVD_CONF.unlink(missing_ok=True)
        if new_state != prev_state or not STATE_FILE.exists():
            save_state(new_state)
        return

    now = time.time()
    if upstream["link_type"] == ARPHRD_RAWIP and upstream["prefix64"]:
        new_state = {
            "mode": "rfc7278",
            "enabled_forwarding": True,
            "shared_iface": shared_iface,
            "prefix": upstream["prefix64"],
            "gateway": upstream["gateway"],
            "up_iface": upstream["iface"],
            "proxy_addrs": upstream["addrs"],
        }
    else:
        new_state = {
            "mode": "ula",
            "enabled_forwarding": True,
            "shared_iface": shared_iface,
            "prefix": ULA_PREFIX,
            "gateway": None,
            "up_iface": upstream["iface"],
            "proxy_addrs": [],
        }

    dep_prefix, dep_until = compute_deprecation(prev_state, new_state["prefix"], now)
    if dep_prefix:
        new_state["deprecated_prefix"] = dep_prefix
        new_state["deprecated_until"] = dep_until

    net_changed = kernel_net_config(prev_state) != kernel_net_config(new_state)
    new_conf = build_radvd_conf(shared_iface, new_state["prefix"], shared_ll, deprecate_prefix=dep_prefix)
    try:
        old_conf = RADVD_CONF.read_text()
    except OSError:
        old_conf = None

    try:
        if net_changed:
            clear_dynamic_v6(prev_state)
            apply_kernel_net(new_state)
        if not radvd_running() or new_conf != old_conf:
            stop_radvd()
            RADVD_CONF.write_text(new_conf)
            run([RADVD_BIN, "-C", str(RADVD_CONF), "-p", str(RADVD_PID), "-m", "syslog"], check=True)
    except Exception:
        stop_radvd()
        clear_dynamic_v6(new_state)
        apply_nft(nat66_up_iface=None)
        STATE_FILE.unlink(missing_ok=True)
        raise

    if new_state != prev_state or not STATE_FILE.exists():
        save_state(new_state)


def main():
    event_action = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("NM_DISPATCHER_ACTION", "")
    if event_action and event_action not in RELEVANT_ACTIONS:
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        reconcile()


if __name__ == "__main__":
    main()
