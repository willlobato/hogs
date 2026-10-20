import sys
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hogs  # noqa: E402


class ParseSizeTest(unittest.TestCase):
    def test_docker_formats(self):
        self.assertEqual(hogs.parse_size("1.4GB"), 1_400_000_000)
        self.assertEqual(hogs.parse_size("77.65kB"), 77_650)
        self.assertEqual(hogs.parse_size("1e+03MB"), 1_000_000_000)
        self.assertEqual(hogs.parse_size("49.63GB (58%)"), 49_630_000_000)
        self.assertEqual(hogs.parse_size("0B"), 0)

    def test_garbage_and_negative(self):
        self.assertEqual(hogs.parse_size(""), 0)
        self.assertEqual(hogs.parse_size("N/A"), 0)
        self.assertEqual(hogs.parse_size("-1.476e+09B"), 0)


class FmtTest(unittest.TestCase):
    def test_uses_decimal_comma(self):
        self.assertEqual(hogs.fmt(12_340_000_000), "12,3 GB")
        self.assertEqual(hogs.fmt(450_000_000), "450 MB")
        self.assertEqual(hogs.fmt(5_000), "5 kB")


class ImageOriginTest(unittest.TestCase):
    def test_origins(self):
        self.assertEqual(hogs.image_origin("<none>"), "none")
        self.assertEqual(hogs.image_origin("registry.gitlab.com/acme/app"), "registry")
        self.assertEqual(hogs.image_origin("123.dkr.ecr.us-east-1.amazonaws.com/app"), "registry")
        self.assertEqual(hogs.image_origin("localhost:5000/app"), "registry")
        self.assertEqual(hogs.image_origin("grafana/grafana"), "hub")
        self.assertEqual(hogs.image_origin("nginx"), "hub")
        self.assertEqual(hogs.image_origin("my-local-build"), "local")


class SelectionTest(unittest.TestCase):
    def setUp(self):
        risks = [hogs.SAFE, hogs.SAFE, hogs.REDOWN, hogs.LOSS, hogs.REDOWN]
        self.items = [hogs.Item("x", f"i{n}", 1, r) for n, r in enumerate(risks, 1)]

    def labels(self, raw):
        return [i.label for i in hogs.parse_selection(raw, self.items)]

    def test_all_safe(self):
        self.assertEqual(self.labels("a"), ["i1", "i2"])

    def test_ranges_and_lists(self):
        self.assertEqual(self.labels("1, 3-4"), ["i1", "i3", "i4"])
        self.assertEqual(self.labels("5 2"), ["i2", "i5"])

    def test_ignores_out_of_range_and_junk(self):
        self.assertEqual(self.labels("99 x 0"), [])
        self.assertEqual(self.labels(""), [])


class AgoTest(unittest.TestCase):
    def days_ago(self, n):
        return hogs.ago(datetime.now(timezone.utc) - timedelta(days=n, hours=1))

    def test_ranges(self):
        self.assertEqual(hogs.ago(None), "sem registro")
        self.assertEqual(self.days_ago(0), "hoje")
        self.assertEqual(self.days_ago(1), "ontem")
        self.assertEqual(self.days_ago(10), "há 10 dias")
        self.assertEqual(self.days_ago(120), "há 4 meses")
        self.assertEqual(self.days_ago(365), "há 1 ano")
        self.assertEqual(self.days_ago(400), "há 1 ano e 1 mês")
        self.assertEqual(self.days_ago(800), "há 2 anos e 2 meses")


class DockerCleanItemsTest(unittest.TestCase):
    def test_never_offers_used_images_or_running_projects(self):
        d = hogs.Docker(running=True, build_cache=5_000_000_000)
        d.images = [
            {"name": "app:dev", "size": 900_000_000, "used": True, "origin": "local", "created": "", "id": "a"},
            {"name": "old:t1", "size": 900_000_000, "used": False, "origin": "local", "created": "", "id": "b"},
        ]
        d.stopped = [
            {"name": "keycloak", "size": 1, "age": 90, "project": "infra", "project_running": True},
            {"name": "ancient", "size": 1, "age": 400, "project": None, "project_running": False},
        ]
        labels = {i.label: i for i in hogs.docker_clean_items(d)}

        self.assertNotIn("Imagem app:dev", labels)
        self.assertIs(labels["Imagem old:t1"].risk, hogs.LOSS)
        self.assertIs(labels["Docker: cache de build"].risk, hogs.SAFE)
        # container de projeto com algo rodando nunca entra no grupo "seguro"
        self.assertIs(labels["Docker: container keycloak"].risk, hogs.REDOWN)
        self.assertIn("(1)", next(k for k in labels if k.startswith("Docker: containers parados")))

    def test_nothing_when_docker_is_down(self):
        self.assertEqual(hogs.docker_clean_items(hogs.Docker(running=False)), [])


class BuilderTest(unittest.TestCase):
    builders = [{"name": "multiarch", "driver": "docker-container", "nodes": ["multiarch0"], "running": True},
                {"name": "default", "driver": "docker", "nodes": ["default"], "running": True},
                {"name": "desktop-linux", "driver": "docker", "nodes": ["desktop-linux"], "running": True}]

    def test_state_volume_maps_to_builder(self):
        self.assertEqual(hogs.builder_of_volume("buildx_buildkit_multiarch0_state", self.builders), "multiarch")
        self.assertIsNone(hogs.builder_of_volume("pgdata", self.builders))

    def test_builder_cache_is_offered_as_safe_prune(self):
        d = hogs.Docker(running=True)
        d.builder_caches = [{"builder": "multiarch", "volume": "buildx_buildkit_multiarch0_state", "size": 15 * 10**9}]
        item = next(i for i in hogs.docker_clean_items(d) if "multiarch" in i.label)
        self.assertIs(item.risk, hogs.SAFE)

    def test_trim_prunes_container_builders_and_only_the_current_context(self):
        calls = []

        def fake_sh(cmd, timeout=60):
            calls.append(cmd)
            out = "desktop-linux\n" if cmd[:3] == ["docker", "context", "show"] else "Total:\t1.5GB\n"
            return mock.Mock(returncode=0, stdout=out, stderr="")

        with mock.patch.object(hogs, "sh", fake_sh), mock.patch.object(hogs, "list_builders", return_value=self.builders):
            done = hogs.trim_builders(20)
        self.assertEqual(done, [("multiarch", 1_500_000_000, ""), ("desktop-linux", 1_500_000_000, "")])
        prunes = [c for c in calls if "prune" in c]
        self.assertTrue(all("--max-used-space" in c and "20gb" in c for c in prunes))

    def test_trim_reports_errors(self):
        def fake_sh(cmd, timeout=60):
            if "prune" in cmd:
                return mock.Mock(returncode=1, stdout="", stderr="ERROR: boom")
            return mock.Mock(returncode=0, stdout="desktop-linux\n", stderr="")

        with mock.patch.object(hogs, "sh", fake_sh), mock.patch.object(hogs, "list_builders", return_value=self.builders[:1]):
            self.assertEqual(hogs.trim_builders(20), [("multiarch", 0, "ERROR: boom")])


class HistoryTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(hogs, "DATA_DIR", tmp), mock.patch.object(hogs, "HISTORY", tmp / "h.jsonl")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def test_append_drops_records_older_than_retention(self):
        hogs.history_append({"t": self.now - 200 * 86400, "free": 1})
        hogs.history_append({"t": self.now, "free": 2})
        self.assertEqual([r["free"] for r in hogs.history_load()], [2])
        self.assertEqual(len(hogs.HISTORY.read_text().splitlines()), 1)

    def test_baseline_prefers_newest_report_at_least_a_day_old(self):
        h = 3600
        records = [{"t": self.now - 72 * h, "full": True}, {"t": self.now - 30 * h, "full": True},
                   {"t": self.now - 2 * h, "full": True}, {"t": self.now - 1 * h, "free": 1}]
        self.assertEqual(hogs.baseline(records, self.now)["t"], self.now - 30 * h)

    def test_baseline_falls_back_to_oldest_report_over_an_hour(self):
        records = [{"t": self.now - 5 * 3600, "full": True}, {"t": self.now - 600, "full": True}]
        self.assertEqual(hogs.baseline(records, self.now)["t"], self.now - 5 * 3600)
        self.assertIsNone(hogs.baseline([{"t": self.now - 600, "full": True}], self.now))

    def test_growth_counts_new_items_and_ignores_small_or_shrinking(self):
        rows = hogs.growth({"docker": 30 * 10**9, "npm": 4 * 10**9, "new": 2 * 10**9, "tiny": 10**9},
                           {"docker": 13 * 10**9, "npm": 5 * 10**9, "tiny": 10**9 - 10})
        self.assertEqual(rows, [("docker", 17 * 10**9), ("new", 2 * 10**9)])

    def test_value_at_picks_last_record_before_time(self):
        records = [{"t": 10, "free": 1}, {"t": 20}, {"t": 30, "free": 3}, {"t": 40, "free": 4}]
        self.assertEqual(hogs.value_at(records, "free", 35), (30, 3))
        self.assertEqual(hogs.value_at(records, "free", 5), (10, 1))
        self.assertIsNone(hogs.value_at(records, "docker_raw", 35))

    def test_sparkline(self):
        records = [{"t": self.now - (6 - d) * 86400, "free": d} for d in range(7)]
        line = hogs.sparkline(records, "free", self.now, days=7, buckets=7)
        self.assertEqual(line[0], "▁")
        self.assertEqual(line[-1], "█")
        self.assertEqual(hogs.sparkline(records[:1], "free", self.now), "")

    def test_fmt_delta(self):
        self.assertEqual(hogs.fmt_delta(17 * 10**9), "+17,0 GB")
        self.assertEqual(hogs.fmt_delta(-3_200_000_000), "−3,2 GB")


if __name__ == "__main__":
    unittest.main()
