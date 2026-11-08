"""Unit tests for latanda-exporter. They need no chain and no network.

Run from monitoring/ after building the image:
  docker run --rm -v "$PWD/exporter:/t:ro" --entrypoint python latanda-exporter:1.0.0 -m unittest discover -s /t
"""

import os
import time
import unittest
import urllib.error
from unittest import mock

import latanda_exporter
from latanda_exporter import Collector, Poller, clean_label

# (chain-controlled input, expected label)
ATTACKS = [
    ("@everyone free tokens", "everyone free tokens"),
    ("<@&123456> vote now", "&123456 vote now"),
    ("[claim](https://evil.example/x)", "claim"),
    ("[https://good.example](https://evil.example)", "unnamed"),
    ("[a](http://x)[b](http://y)", "ab"),
    ("HTTPS://EVIL.EXAMPLE/a?b=c", "unnamed"),
    ("see www.evil.example now", "see now"),
    ("evil.example", "evil example"),
    ("discord.gg/abc123", "unnamed"),
    ("http: //evil.example", "unnamed"),
    ("http:\\\\evil.example", "evil example"),
    ("//evil.example/x", "unnamed"),
    # Zero width space inside "http".
    ("ht\u200btp://evil.example", "unnamed"),
    # Fullwidth forms, which look the same in Discord.
    ("\uff20everyone \uff48\uff54\uff54\uff50://evil.example", "everyone"),
    # Removing the star must not leave a working link behind.
    ("htt*p://evil.example", "htt"),
    ("||spoiler|| `code` **bold**", "spoiler code bold"),
]

UNCHANGED = [
    "Raise max validators",
    "Upgrade to v1.2.3",
    "validator_1",
    "Proposal (v2): 5% fee, 10.5 LTD",
    "节点一号",
    "AlxVoy ⚡",
]


class CleanLabelTest(unittest.TestCase):
    def test_attacks(self):
        for raw, expected in ATTACKS:
            with self.subTest(raw=raw):
                self.assertEqual(clean_label(raw), expected)

    def test_nothing_clickable_survives(self):
        for raw, _ in ATTACKS:
            with self.subTest(raw=raw):
                cleaned = clean_label(raw).lower()
                for token in ("http", "://", "@", "](", "<", "www.", "evil.example"):
                    self.assertNotIn(token, cleaned)

    def test_normal_names_stay(self):
        for name in UNCHANGED:
            with self.subTest(name=name):
                self.assertEqual(clean_label(name), name)

    def test_idempotent(self):
        for raw in [a for a, _ in ATTACKS] + UNCHANGED:
            with self.subTest(raw=raw):
                once = clean_label(raw)
                self.assertEqual(clean_label(once), once)

    def test_fallback_for_empty_values(self):
        for empty in (None, "", "   ", "\x00\x01", "@@@"):
            with self.subTest(value=empty):
                self.assertEqual(clean_label(empty), "unnamed")
        self.assertEqual(clean_label("", fallback="proposal 7"), "proposal 7")

    def test_length_limit(self):
        self.assertEqual(clean_label("a" * 100), "a" * 64)
        self.assertEqual(clean_label("a" * 63 + " b c"), "a" * 63)

    def test_huge_value_stays_fast(self):
        started = time.monotonic()
        for raw in ("[" * 10**6, "a." * 10**6, "/" * 10**6, "\ufdfa" * 10**6):
            clean_label(raw)
        self.assertLess(time.monotonic() - started, 5)


class FakeClient:
    """Answers the REST calls of Poller.collect_chain_state, and RPC calls if rpc_url is set."""

    def __init__(self, validators=None, proposals=None, rpc_url=""):
        self.rpc_url = rpc_url
        self.rpc_error = None     # raised by every RPC call while set
        self.rpc_calls = 0
        self.responses = {
            "/cosmos/slashing/v1beta1/params": {"params": {
                "signed_blocks_window": "10000",
                "min_signed_per_window": "0.5",
                "downtime_jail_duration": "600s",
            }},
            "/cosmos/staking/v1beta1/pool": {"pool": {"bonded_tokens": "1000000", "not_bonded_tokens": "0"}},
            "/cosmos/staking/v1beta1/validators": {"validators": validators if validators is not None else []},
            "/cosmos/slashing/v1beta1/signing_infos": {"info": []},
            "/cosmos/gov/v1/proposals": {"proposals": proposals if proposals is not None else []},
            "/cosmos/base/tendermint/v1beta1/blocks/latest": {"block": {"header": {
                "height": "5", "time": "2026-11-03T00:00:00Z",
            }}},
        }

    def rest(self, path, **_params):
        return self.responses[path]

    def rpc(self, path, **params):
        """A chain at height 7 with one block per second."""
        self.rpc_calls += 1
        if self.rpc_error:
            raise self.rpc_error
        if path == "/status":
            return {"result": {"sync_info": {"latest_block_height": "7"}}}
        if path == "/blockchain":
            heights = range(int(params["minHeight"]), int(params["maxHeight"]) + 1)
            return {"result": {"block_metas": [{"header": {
                "height": str(h), "time": f"2026-11-03T00:00:{h:02d}Z", "proposer_address": "AB",
            }} for h in heights]}}
        raise AssertionError(f"unexpected RPC call {path}")


class StopLoop(BaseException):
    """Ends the endless Poller.run; not an Exception, so run() cannot catch it."""


def run_polls(poller, polls=1):
    sleeps = mock.Mock(side_effect=[None] * (polls - 1) + [StopLoop()])
    with mock.patch.object(latanda_exporter.time, "sleep", sleeps):
        try:
            poller.run()
        except StopLoop:
            pass


def exported(poller):
    """Metric name -> list of (labels, value), as Prometheus would scrape them."""
    out = {}
    for family in Collector(poller).collect():
        for sample in family.samples:
            out.setdefault(sample.name, []).append((sample.labels, sample.value))
    return out


VALIDATOR = {"operator_address": "ltdvaloper1test", "tokens": "9000000",
             "description": {"moniker": "heimnetz"}}


class PollerTest(unittest.TestCase):
    def test_labels_are_cleaned_on_the_way_in(self):
        validator = {"operator_address": "ltdvaloper1test", "tokens": "1000000",
                     "description": {"moniker": "@everyone [claim](https://evil.example/x)"}}
        proposals = [{"id": "1", "title": "Vote at evil.example/x @everyone"},
                     {"id": "2", "title": "https://evil.example", "summary": "ignored"},
                     {"id": "3"}]
        client = FakeClient(validators=[validator], proposals=proposals)
        snapshot = Poller(client, [], 30).collect_chain_state()
        self.assertEqual(snapshot["validators"][0]["moniker"], "everyone claim")
        self.assertEqual([p["title"] for p in snapshot["proposals"]],
                         ["Vote at everyone", "proposal 2", "proposal 3"])

    def test_malformed_response_does_not_end_the_process(self):
        # "validators": null raises TypeError, which the old except clause let through.
        malformed = FakeClient()
        malformed.responses["/cosmos/staking/v1beta1/validators"] = {"validators": None}
        poller = Poller(malformed, [], 30)
        poller.up = 1
        with self.assertLogs("latanda_exporter", level="WARNING") as logs:
            run_polls(poller)
        self.assertEqual(poller.up, 0)
        self.assertIn("TypeError", logs.output[0])

    def test_poller_recovers_after_a_bad_response(self):
        client = FakeClient()
        good = client.responses["/cosmos/staking/v1beta1/validators"]
        client.responses["/cosmos/staking/v1beta1/validators"] = {"validators": None}
        poller = Poller(client, [], 30)
        with self.assertLogs("latanda_exporter", level="WARNING"):
            run_polls(poller)
        self.assertEqual(poller.up, 0)
        client.responses["/cosmos/staking/v1beta1/validators"] = good
        run_polls(poller)
        self.assertEqual(poller.up, 1)


class RpcTest(unittest.TestCase):
    """CHAIN_RPC_URL is optional: a failing RPC must not cost the REST data."""

    @staticmethod
    def refused_client():
        # A central instance with no local node and CHAIN_RPC_URL left at its default.
        client = FakeClient(validators=[VALIDATOR], rpc_url="http://localhost:26657")
        client.rpc_error = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return client

    def test_rpc_failure_keeps_rest_data(self):
        poller = Poller(self.refused_client(), [], 30)
        with self.assertLogs("latanda_exporter", level="WARNING") as logs:
            run_polls(poller, polls=2)
        self.assertEqual(len(logs.records), 1, "one warning per outage, not one per poll")
        self.assertIn("CHAIN_RPC_URL empty", logs.output[0])
        metrics = exported(poller)
        self.assertEqual(metrics["latanda_exporter_up"][0][1], 1)
        self.assertEqual(metrics["latanda_exporter_rpc_up"][0][1], 0)
        self.assertEqual([labels["moniker"] for labels, _ in metrics["latanda_validator_jailed"]],
                         ["heimnetz"])
        self.assertEqual(metrics["latanda_validator_voting_power"][0][1], 9)
        self.assertEqual(metrics["latanda_chain_latest_block_height"][0][1], 5)  # from REST
        self.assertNotIn("latanda_chain_block_time_seconds", metrics)

    def test_rpc_failure_after_success_drops_block_time(self):
        client = FakeClient(validators=[VALIDATOR], rpc_url="http://rpc.example:26657")
        poller = Poller(client, [], 30)
        run_polls(poller)
        self.assertEqual(exported(poller)["latanda_chain_block_time_seconds"][0][1], 1.0)
        client.rpc_error = TimeoutError("timed out")
        with self.assertLogs("latanda_exporter", level="WARNING") as logs:
            run_polls(poller)
        self.assertIn("TimeoutError", logs.output[0])
        metrics = exported(poller)
        self.assertEqual(metrics["latanda_exporter_up"][0][1], 1)
        self.assertEqual(metrics["latanda_exporter_rpc_up"][0][1], 0)
        self.assertEqual(metrics["latanda_chain_latest_block_height"][0][1], 5)  # from REST
        self.assertNotIn("latanda_chain_block_time_seconds", metrics)

    def test_rpc_recovers(self):
        client = self.refused_client()
        poller = Poller(client, [], 30)
        with self.assertLogs("latanda_exporter", level="WARNING"):
            run_polls(poller)
        client.rpc_error = None
        with self.assertLogs("latanda_exporter", level="INFO") as logs:
            run_polls(poller)
        self.assertIn("answers again", logs.output[-1])
        metrics = exported(poller)
        self.assertEqual(metrics["latanda_exporter_rpc_up"][0][1], 1)
        self.assertEqual(metrics["latanda_chain_latest_block_height"][0][1], 7)  # from RPC
        self.assertEqual(metrics["latanda_chain_block_time_seconds"][0][1], 1.0)

    def test_rest_failure_still_fails_the_poll(self):
        client = self.refused_client()
        del client.responses["/cosmos/base/tendermint/v1beta1/blocks/latest"]
        poller = Poller(client, [], 30)
        with self.assertLogs("latanda_exporter", level="WARNING"):
            run_polls(poller)
        self.assertEqual(poller.up, 0)

    def test_empty_rpc_url_never_calls_rpc(self):
        client = FakeClient(validators=[VALIDATOR])
        poller = Poller(client, [], 30)
        with self.assertNoLogs("latanda_exporter", level="WARNING"):
            run_polls(poller, polls=2)
        self.assertEqual(client.rpc_calls, 0)
        metrics = exported(poller)
        self.assertEqual(metrics["latanda_exporter_up"][0][1], 1)
        self.assertNotIn("latanda_exporter_rpc_up", metrics)
        self.assertNotIn("latanda_validator_blocks_proposed_total", metrics)
        self.assertEqual(metrics["latanda_chain_latest_block_height"][0][1], 5)

    def test_empty_env_value_switches_rpc_off(self):
        # docker-compose.yml passes CHAIN_RPC_URL with "-", not ":-", so an empty
        # value in .env reaches the exporter as an empty string.
        with mock.patch.dict(os.environ, {"CHAIN_RPC_URL": ""}):
            self.assertEqual(latanda_exporter.env_str("CHAIN_RPC_URL", "http://localhost:26657"), "")


if __name__ == "__main__":
    unittest.main()
