"""Regression cases for a working tunnel whose exit IP is rejected by a service."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from service_routing import DEFAULT_POLICY, TRANSPORT_PROBE, apply_policy, apply_clash_policy, load_policy


class ServiceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.policy = copy.deepcopy(DEFAULT_POLICY)
        self.env = {"SERVER_IP": "192.0.2.1", "EXIT2_HOST": "192.0.2.2", "EXIT2_DOMAIN": "exit2.example.com"}
        self.relay = {"tag": "to-exit", "protocol": "vless", "settings": {"vnext": [
            {"address": "192.0.2.1", "users": [{"id": "test-relay"}]}]},
            "streamSettings": {"realitySettings": {"serverName": "exit1.example.com"}}}
        self.exit = {"outbounds": [{"tag": "direct", "protocol": "freedom"}], "routing": {"rules": [
            {"ip": ["geoip:private"], "outboundTag": "block"},
            {"protocol": ["bittorrent"], "outboundTag": "block"}]}}
        self.entry = {"inbounds": [{"tag": "xhttp-reality"}],
            "outbounds": [self.relay, {"tag": "standby-exit"}],
            "routing": {"rules": [{"balancerTag": "exits"}],
                        "balancers": [{"tag": "exits", "selector": ["to-exit"]}]},
            "observatory": {"subjectSelector": ["to-exit", "standby-exit"]}}

    def test_primary_forwards_without_weakening_block_rules(self):
        c = apply_policy(self.exit, self.policy, "exit", self.env, self.relay)
        self.assertEqual(c["routing"]["rules"][:2], self.exit["routing"]["rules"])
        self.assertEqual(c["routing"]["rules"][-1]["outboundTag"], "service-standby")
        o = c["outbounds"][-1]
        self.assertEqual(o["settings"]["vnext"][0]["address"], "192.0.2.2")
        self.assertEqual(o["streamSettings"]["realitySettings"]["serverName"], "exit2.example.com")
        self.assertEqual(self.relay["settings"]["vnext"][0]["address"], "192.0.2.1")

    def test_standby_and_single_exit_cannot_loop_or_fall_back_to_ipv6(self):
        c = apply_policy(self.exit, self.policy, "exit", {"SERVER_IP": "192.0.2.2"})
        self.assertEqual(c["routing"]["rules"][-1]["outboundTag"], "service-ipv4")
        self.assertEqual(c["outbounds"][-1]["sendThrough"], "192.0.2.2")
        self.assertEqual(c["outbounds"][-1]["settings"]["domainStrategy"], "ForceIPv4")
        self.assertFalse(any(o["protocol"] == "vless" for o in c["outbounds"]))

    def test_entry_applies_to_all_users_before_default_route(self):
        c = apply_policy(self.entry, self.policy, "entry", self.env)
        r = c["routing"]["rules"][0]
        self.assertEqual(r["outboundTag"], "standby-exit")
        self.assertNotIn("user", r)
        self.assertIn("quic", c["inbounds"][0]["sniffing"]["destOverride"])
        self.assertEqual(c["routing"]["rules"][1:], self.entry["routing"]["rules"])

    def test_explicit_primary_and_disabled_policy(self):
        self.policy["preferred_exit"] = "primary"
        c = apply_policy(self.entry, self.policy, "entry", self.env)
        self.assertEqual(c["routing"]["rules"][0]["outboundTag"], "to-exit")
        self.policy["domains"] = []
        self.assertEqual(apply_policy(self.exit, self.policy, "exit", {}), self.exit)

    def test_optional_failover_has_only_one_preferred_exit(self):
        self.policy["fallback_to_primary"] = True
        for role, base in (("entry", self.entry), ("exit", self.exit)):
            c = apply_policy(base, self.policy, role, self.env, self.relay)
            b = c["routing"]["balancers"][-1]
            matches = [o["tag"] for o in c["outbounds"] if any(o["tag"].startswith(s) for s in b["selector"])]
            self.assertEqual(len(matches), 1)
            self.assertNotIn(b["fallbackTag"], matches)
            self.assertIn(matches[0], c["observatory"]["subjectSelector"])

    def test_policy_validation_and_extra_services(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "policy.json"
            self.assertEqual(load_policy(path), DEFAULT_POLICY)
            for bad in ({"domains": "google.com"}, {"domains": ["*.google.com"]},
                        {"domains": ["https://google.com"]}, {"fallback_to_primary": "false"},
                        {"preferred_exit": "typo"}, {"unknown": True}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    load_policy(path)
            path.write_text(json.dumps({"domains": ["google.com", "openai.com", "openai.com"]}))
            self.assertEqual(load_policy(path)["domains"], ["google.com", "openai.com"])
        self.assertEqual(load_policy(ROOT / "service-routing.example.json"), DEFAULT_POLICY)

    def test_fast_mode_and_geoip_cannot_bypass_service_route(self):
        c = {"proxy-groups": [], "rules": ["GEOIP,RU,DIRECT", "MATCH,FAST"]}
        apply_clash_policy(c, self.policy, ["RU", "NL", "NL2"])
        self.assertEqual(c["rules"][0], "DOMAIN-SUFFIX,google.com,🌍 Сервисы")
        self.assertEqual(c["proxy-groups"][0]["proxies"], ["RU", "NL", "NL2"])
        from urllib.parse import urlparse
        host = urlparse(TRANSPORT_PROBE).hostname
        self.assertFalse(any(host == d or host.endswith("." + d) for d in self.policy["domains"]))

    def test_generated_normal_and_game_subscriptions_preserve_service_priority(self):
        spec = importlib.util.spec_from_file_location("clash_sub", ROOT / "bin/clash-sub.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mod.ETC = root / "etc"; mod.CL = root / "clients"; mod.OUT = root / "out"
            mod.ETC.mkdir(); (mod.CL / "Test").mkdir(parents=True)
            (mod.ETC / "vpn.env").write_text("DOMAIN=exit.example.com\nSERVER_IP=192.0.2.1\nRU_HOST=192.0.2.3\nEXIT2_HOST=192.0.2.2\nEXIT2_DOMAIN=exit2.example.com\n")
            (mod.ETC / "secrets.env").write_text("REALITY_PUB=public\nSHORT_ID=abcd\nXHTTP_PATH=path\n")
            (mod.ETC / "users.json").write_text(json.dumps([{"name": "Test", "uuid": "test", "sub": "token"}]))
            mod.yaml = None
            with patch.object(sys, "argv", ["clash-sub.py", "Test"]), patch.object(mod.subprocess, "run"):
                mod.main()
            for token in ("token", "token-game"):
                c = json.loads((mod.OUT / token).read_text(encoding="utf-8"))
                self.assertEqual(c["rules"][0], "DOMAIN-SUFFIX,google.com,🌍 Сервисы")
                names = {p["name"] for p in c["proxies"]}
                g = next(g for g in c["proxy-groups"] if g["name"] == "🌍 Сервисы")
                self.assertTrue(set(g["proxies"]) <= names)
                self.assertFalse(any("AWG" in n for n in g["proxies"]))
                ordinary = next(g for g in c["proxy-groups"] if g["name"] == "⚡ Авто")
                self.assertEqual(ordinary["url"], TRANSPORT_PROBE)


if __name__ == "__main__":
    unittest.main()
