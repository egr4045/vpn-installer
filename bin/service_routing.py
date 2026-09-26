#!/usr/bin/env python3
"""Shared Xray/Clash policy for services that reject an otherwise healthy exit IP."""
import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re

DEFAULT_POLICY = {
    "domains": ["google.com", "googleapis.com", "gstatic.com", "googleusercontent.com"],
    "preferred_exit": "standby",
    "fallback_to_primary": False,
}
POLICY_PATH = Path("/etc/safechill/service-routing.json")
TRANSPORT_PROBE = "https://cp.cloudflare.com/generate_204"


def load_policy(path=POLICY_PATH):
    policy = copy.deepcopy(DEFAULT_POLICY)
    if Path(path).exists():
        custom = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(custom, dict) or set(custom) - set(policy):
            raise ValueError("service-routing.json: unknown fields or non-object policy")
        policy.update(custom)
    domains = policy["domains"]
    if not isinstance(domains, list) or any(
        not isinstance(d, str) or len(d) > 253 or "." not in d
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
               for label in d.split(".")) for d in domains
    ):
        raise ValueError("domains must be lowercase DNS suffixes, without wildcards or URLs")
    policy["domains"] = list(dict.fromkeys(domains))
    if policy["preferred_exit"] not in ("primary", "standby"):
        raise ValueError("preferred_exit must be primary or standby")
    if type(policy["fallback_to_primary"]) is not bool:
        raise ValueError("fallback_to_primary must be boolean")
    return policy


def apply_policy(config, policy, role, env, relay=None):
    """Apply to freshly rendered configs; relay is the rendered RU outbound to primary."""
    c = copy.deepcopy(config)
    if not policy["domains"]:
        return c
    domains = ["domain:" + d for d in policy["domains"]]
    rule = {"type": "field", "domain": domains}
    routing = c.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    standby = policy["preferred_exit"] == "standby" and bool(env.get("EXIT2_HOST"))
    if role == "entry":
        target = "standby-exit" if standby else "to-exit"
        if not any(o["tag"] == target for o in c["outbounds"]):
            raise ValueError("service routing outbound missing: " + target)
        fallback = "to-exit"
        # Recognize TLS/QUIC names even if the client sends an IP destination.
        for inbound in c["inbounds"]:
            inbound["sniffing"] = {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        insert_at = 0
    else:
        # ForceIPv4 does not fall back to the system resolver/IPv6 on an A lookup failure.
        # Binding the IPv4 source also rejects unsniffable literal IPv6 destinations.
        source = str(ipaddress.IPv4Address(env["SERVER_IP"]))
        c["outbounds"].append({"tag": "service-ipv4", "protocol": "freedom",
                               "sendThrough": source, "settings": {"domainStrategy": "ForceIPv4"}})
        target = fallback = "service-ipv4"
        if standby:
            if relay is None:
                raise ValueError("rendered relay required for standby service route")
            outbound = copy.deepcopy(relay)
            outbound["tag"] = target = "service-standby"
            outbound["settings"]["vnext"][0]["address"] = env["EXIT2_HOST"]
            outbound["streamSettings"]["realitySettings"]["serverName"] = env["EXIT2_DOMAIN"]
            c["outbounds"].append(outbound)
        # Preserve private-address and BitTorrent blocking before service exceptions.
        insert_at = len(rules)
    if standby and policy["fallback_to_primary"]:
        routing.setdefault("balancers", []).append({
            "tag": "service-exits", "selector": [target], "fallbackTag": fallback,
            "strategy": {"type": "leastPing"},
        })
        obs = c.setdefault("observatory", {"subjectSelector": [],
            "probeUrl": TRANSPORT_PROBE, "probeInterval": "20s",
            "enableConcurrency": True})
        if target not in obs["subjectSelector"]:
            obs["subjectSelector"].append(target)
        rule["balancerTag"] = "service-exits"
    else:
        rule["outboundTag"] = target
    rules.insert(insert_at, rule)
    return c


def apply_clash_policy(config, policy, auto_order):
    if not policy["domains"]:
        return
    # Never let FAST/AWG bypass the server's service policy. RU stays first for mobile
    # allowlists; either primary Xray or RU forwards selected services to the same exit.
    group = "🌍 Сервисы"
    config["proxy-groups"].append({"name": group, "type": "fallback", "hidden": True,
        "proxies": list(auto_order), "url": "https://www.gstatic.com/generate_204",
        "interval": 120, "timeout": 3000, "lazy": False})
    # Ahead of GEOIP,RU and process DIRECT exceptions: a Russian CDN answer must not leak.
    config["rules"][:0] = [f"DOMAIN-SUFFIX,{d},{group}" for d in policy["domains"]]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy", type=Path, default=POLICY_PATH)
    p.add_argument("--dump-policy", action="store_true")
    p.add_argument("--role", choices=("exit", "entry"))
    p.add_argument("--relay", type=Path)
    p.add_argument("input", nargs="?", type=Path)
    p.add_argument("output", nargs="?", type=Path)
    args = p.parse_args()
    policy = load_policy(args.policy)
    if args.dump_policy:
        print(json.dumps(policy, indent=2))
        return
    if not args.role or not args.input or not args.output:
        p.error("--role, input and output required")
    relay = None
    if args.relay:
        relay = next(o for o in json.loads(args.relay.read_text())["outbounds"] if o["tag"] == "to-exit")
    result = apply_policy(json.loads(args.input.read_text()), policy, args.role, os.environ, relay)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
