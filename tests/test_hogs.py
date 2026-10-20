import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
