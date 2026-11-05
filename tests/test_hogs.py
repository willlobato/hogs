import io
import sys
import json
import os
import plistlib
import subprocess
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

    def test_rounding_never_shows_1000_of_the_smaller_unit(self):
        self.assertEqual(hogs.fmt(999_700_000), "1,0 GB")
        self.assertEqual(hogs.fmt(999_400_000), "999 MB")
        self.assertEqual(hogs.fmt(999_700), "1 MB")


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

    def test_huge_range_is_clamped_to_the_list(self):
        start = time.perf_counter()
        self.assertEqual(self.labels("4-99999999999999"), ["i4", "i5"])
        self.assertEqual(hogs.selection_numbers("0-99999999999999", 3), {1, 2, 3})
        self.assertLess(time.perf_counter() - start, 0.5)


class ParseGbTest(unittest.TestCase):
    def test_accepts_decimal_comma_and_rejects_non_finite(self):
        self.assertEqual(hogs.parse_gb("2,5", "uso"), 2.5)
        for bad in ("nan", "inf", "0", "-3", "x"):
            with self.assertRaises(SystemExit, msg=bad):
                hogs.parse_gb(bad, "uso")


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

    @staticmethod
    def fake_docker(images=(), containers=(), inspect=None):
        """sh falso para gather_docker: system df (resumo e -v), inspect; o resto responde vazio."""
        calls = []

        def sh(cmd, timeout=60):
            calls.append(cmd)
            if cmd[:3] == ["docker", "system", "df"]:
                out = (json.dumps({"Images": list(images), "Containers": list(containers), "Volumes": []})
                       if "-v" in cmd else json.dumps({"Type": "Images", "Size": "0B", "Reclaimable": "0B"}))
                return subprocess.CompletedProcess(cmd, 0, out, "")
            if cmd[:2] == ["docker", "inspect"]:
                return inspect and subprocess.CompletedProcess(cmd, 0, inspect, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return sh, calls

    def test_image_pulled_by_digest_is_removed_by_digest_not_by_repo(self):
        # `docker rmi postgres` seria postgres:latest, outra imagem
        digest = "sha256:" + "d" * 64
        img = {"Repository": "postgres", "Tag": "<none>", "Digest": digest, "ID": "sha256:" + "e" * 64,
               "UniqueSize": "600MB", "Containers": "0", "CreatedSince": "2 months ago"}
        sh, calls = self.fake_docker(images=[img])
        with mock.patch.object(hogs, "sh", sh):
            for item in hogs.docker_clean_items(hogs.gather_docker()):
                item.action()
        self.assertEqual([c for c in calls if c[:2] == ["docker", "rmi"]], [["docker", "rmi", f"postgres@{digest}"]])

    def test_paused_container_is_alive_and_protects_its_project(self):
        labels = "com.docker.compose.project=infra"
        cs = [{"ID": "p" * 64, "Names": "db", "State": "paused", "Labels": labels, "Size": "1MB"},
              {"ID": "x" * 64, "Names": "worker", "State": "exited", "Labels": labels, "Size": "1MB"}]
        sh, _ = self.fake_docker(containers=cs, inspect=f"{'x' * 64}|2020-01-01T00:00:00Z|2019-01-01T00:00:00Z\n")
        with mock.patch.object(hogs, "sh", sh):
            d = hogs.gather_docker()
        self.assertEqual([(c["name"], c["project_running"]) for c in d.stopped], [("worker", True)])

    def test_unknown_container_age_never_counts_as_old(self):
        cs = [{"ID": "a" * 64, "Names": "mydb", "State": "exited", "Labels": "", "Size": "1MB"}]
        sh, _ = self.fake_docker(containers=cs, inspect=None)  # docker inspect falhou
        with mock.patch.object(hogs, "sh", sh):
            d = hogs.gather_docker()
        item = next(i for i in hogs.docker_clean_items(d) if "mydb" in i.label)
        self.assertIs(item.risk, hogs.LOSS)
        self.assertEqual(item.hint, "idade desconhecida")

    def test_never_started_container_ages_from_creation(self):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.0Z")
        cs = [{"ID": "a" * 64, "Names": "fresh", "State": "created", "Labels": "", "Size": "1MB"}]
        sh, _ = self.fake_docker(containers=cs, inspect=f"{'a' * 64}|0001-01-01T00:00:00Z|{yesterday}\n")
        with mock.patch.object(hogs, "sh", sh):
            self.assertEqual(hogs.gather_docker().stopped[0]["age"], 1)

    def test_report_rows_for_stopped_containers_use_clean_risks(self):
        d = hogs.Docker(running=True)
        d.stopped = [{"name": "mydb", "size": 5e9, "age": 3, "project": None, "project_running": False},
                     {"name": "ancient", "size": 1e9, "age": 400, "project": None, "project_running": False}]
        risks = {label.split(" (")[0]: risk for label, _, risk, _ in hogs.docker_rows(d) if "parados" in label}
        self.assertEqual(risks, {"Containers parados há +30 dias": hogs.SAFE, "Outros containers parados": hogs.LOSS})

    def test_small_images_hint_ellipsis_only_when_truncated(self):
        d = hogs.Docker(running=True)
        d.images = [{"name": "nginx:1", "size": 100e6, "used": False, "origin": "hub", "created": "", "id": "a"}]
        item = next(i for i in hogs.docker_clean_items(d) if i.label.startswith("Imagens pequenas"))
        self.assertEqual(item.hint, "nginx:1")

    def test_failed_removal_is_reported_not_marked_done(self):
        d = hogs.Docker(running=True)
        d.images = [{"name": "old:t1", "size": 900_000_000, "used": False, "origin": "local", "created": "", "id": "b"}]
        items = hogs.docker_clean_items(d)
        con = hogs.Console(file=io.StringIO(), width=200)
        failing = lambda cmd, timeout=60: subprocess.CompletedProcess(cmd, 1, "", "Error: conflict: image is in use")
        with mock.patch.object(hogs, "sh", failing), mock.patch.object(hogs, "clean_items", return_value=items), \
                mock.patch.object(hogs.Prompt, "ask", return_value="1"), \
                mock.patch.object(hogs.Confirm, "ask", return_value=True):
            hogs.clean(con, hogs.Snapshot(1, 1, d, [], []))
        out = con.file.getvalue().split("Total:")[1]
        self.assertNotIn("✔ Imagem old:t1", out)
        self.assertIn("↷ Imagem old:t1: Error: conflict: image is in use", out)

    def test_run_all_runs_every_command_and_then_fails(self):
        calls = []

        def sh(cmd, timeout=60):
            calls.append(cmd)
            return None if cmd[-1] == "b" else subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(hogs, "sh", sh), self.assertRaisesRegex(RuntimeError, "1 de 3 falharam"):
            hogs.run_all([["docker", "rm", x] for x in "abc"])
        self.assertEqual(len(calls), 3)


class MountsTest(unittest.TestCase):
    def test_under_mount_both_directions(self):
        m = [Path("/u/dev/db/data")]
        self.assertTrue(hogs.under_mount(Path("/u/dev/db/data/x.mdf"), m))
        self.assertTrue(hogs.under_mount(Path("/u/dev/db"), m))
        self.assertFalse(hogs.under_mount(Path("/u/dev/dbx"), m))

    def test_gather_reads_bind_mounts_of_stopped_containers_too(self):
        cs = [{"ID": "a" * 64, "Names": "db", "State": "exited", "Labels": "", "Size": "1MB"}]
        mounts = json.dumps([{"Type": "bind", "Source": "/tmp/dados-db"}, {"Type": "volume", "Source": "/var/lib/x"}])
        def inspect(cmd):
            out = f"{'a' * 64}|2020-01-01T00:00:00Z|2019-01-01T00:00:00Z\n" if "{{.Id}}|" in cmd[3] else mounts + "\n"
            return out
        sh, _ = DockerCleanItemsTest.fake_docker(containers=cs, inspect=None)
        def sh2(cmd, timeout=60):
            if cmd[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(cmd, 0, inspect(cmd), "")
            return sh(cmd, timeout)
        with mock.patch.object(hogs, "sh", sh2):
            d = hogs.gather_docker()
        self.assertTrue(d.mounts_ok)
        self.assertEqual(d.mounts, [Path("/tmp/dados-db").resolve()])

    def test_known_mounts_falls_back_to_saved_then_unknown(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "MOUNTS_FILE", tmp / "m.json"), mock.patch.object(hogs, "DATA_DIR", tmp), \
                mock.patch.object(hogs.shutil, "which", return_value="/usr/bin/docker"):
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([], False))
            hogs.known_mounts(hogs.Docker(running=True, mounts=[Path("/x/db")], mounts_ok=True))
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([Path("/x/db")], True))

    def test_no_docker_installed_means_no_mounts_known_ok(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "MOUNTS_FILE", tmp / "m.json"), mock.patch.object(hogs.shutil, "which", return_value=None):
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([], True))


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


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-b", "main")
        self.git(self.repo, "config", "user.name", "Test")
        self.git(self.repo, "config", "user.email", "test@example.invalid")
        self.git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "file.txt").write_text("initial\n")
        (self.repo / ".gitignore").write_text(".env\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "initial")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.path = self.root / "feature with spaces"
        self.git(self.repo, "worktree", "add", "-b", "feature", str(self.path))
        self.w = hogs.Worktree(self.path, self.repo)
        self.age()

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()

    def age(self):
        old = time.time() - 90 * 86400
        gd = Path(self.git(self.path, "rev-parse", "--absolute-git-dir"))
        for base in (self.path, gd):
            for root, dirs, files in os.walk(base):
                for name in dirs + files:
                    os.utime(Path(root) / name, (old, old))
            os.utime(base, (old, old))

    def inspect(self, usage=None, agents=None, days=30):
        return hogs.inspect_worktree(self.w, days, usage or ([], "", []), agents or hogs.Agents())

    def quiet(self):
        stack = mock.patch.object(hogs, "worktree_usage", return_value=([], "", []))
        stack2 = mock.patch.object(hogs, "agent_activity", return_value=hogs.Agents())
        stack.start(); stack2.start()
        self.addCleanup(stack.stop); self.addCleanup(stack2.stop)

    def test_old_merged_clean_is_eligible_and_removal_preserves_branch(self):
        row = self.inspect()
        self.assertTrue(row.eligible, row.reason)
        self.quiet()
        hogs.remove_worktree(row, 30)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.git(self.repo, "rev-parse", "feature"), row.head)

    def test_modified_untracked_and_ignored_files_are_preserved(self):
        for filename in ("file.txt", "untracked.txt", ".env"):
            with self.subTest(filename=filename):
                p = self.path / filename
                original = p.read_text() if p.exists() else None
                p.write_text("keep me\n")
                self.assertFalse(self.inspect().eligible)
                if original is None:
                    p.unlink()
                else:
                    p.write_text(original)
                self.age()

    def test_unmerged_commit_is_preserved(self):
        (self.path / "file.txt").write_text("new work\n")
        self.git(self.path, "commit", "-am", "work")
        self.age()
        self.assertIn("merge não comprovado", self.inspect().reason)

    def test_recent_locked_and_main_are_preserved(self):
        os.utime(self.path / "file.txt", None)
        self.assertFalse(self.inspect().eligible)
        self.age()
        self.git(self.repo, "worktree", "lock", str(self.path))
        self.assertFalse(self.inspect().eligible)
        self.assertFalse(hogs.inspect_worktree(hogs.Worktree(self.repo, self.repo), 30, ([], "", [])).eligible)

    def test_running_things_and_unknown_checks_are_preserved(self):
        for usage, verdict in ((([(self.path / "file.txt", "vim (pid 1)")], "", []), "depois"),
                               (([], "failed", []), "manter"),
                               (([], "", [(self.root, "container db")]), "depois"),
                               (([], "", [(self.path / "file.txt", "container web")]), "depois")):
            row = self.inspect(usage)
            self.assertFalse(row.eligible)
            self.assertEqual(row.verdict, verdict)
        self.assertEqual(self.inspect(([(self.path / "file.txt", "vim (pid 1)")], "", [])).users, ["vim (pid 1)"])
        self.assertTrue(self.inspect(([(self.root, "zsh (pid 2)")], "", [])).eligible)
        many = ([(self.path / "a", "node (pid 1)"), (self.path / "b", "node (pid 2)"),
                 (self.path, "Claude Code apps-3")], "", [])
        row = self.inspect(many)
        self.assertEqual(row.users, ["Claude Code apps-3 (aberta dentro)", "node ×2"])
        self.assertEqual(row.sessions, ["apps-3"])
        self.assertEqual(row.verdict, "depois", "sessão aberta DENTRO da pasta perderia o cwd")
        self.assertIn("aberta dentro da pasta", row.reason)

    def test_merged_recent_with_open_session_is_safe_by_default(self):
        os.utime(self.path / "file.txt", None)
        cited = hogs.Agents(mentions={self.path: {"Claude Code apps-9d"}})
        row = self.inspect(agents=cited, days=0)
        self.assertTrue(row.eligible, row.reason)
        self.assertEqual(row.sessions, ["apps-9d"])
        self.assertEqual(row.users, ["Claude Code apps-9d"])
        self.assertTrue(row.reason.endswith("sessão apps-9d"))
        row = self.inspect(agents=cited, days=7)
        self.assertEqual(row.verdict, "depois", "--worktree-days continua valendo quando pedido")
        self.assertTrue(row.reason.startswith("mexido"))
        (self.path / "file.txt").write_text("rascunho\n")
        row = self.inspect(agents=cited, days=0)
        self.assertEqual(row.verdict, "manter")
        self.assertEqual(row.users, ["Claude Code apps-9d"], "quem usa aparece mesmo quando é ⚪")

    def test_build_output_is_not_activity(self):
        (self.repo / ".gitignore").write_text(".env\nnode_modules/\n")
        self.git(self.repo, "commit", "-am", "ignore")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        self.git(self.path, "merge", "--ff-only", "main")
        self.age()
        (self.path / "node_modules").mkdir()
        (self.path / "node_modules" / "pkg").write_text("x")
        row = self.inspect()
        self.assertTrue(row.eligible, row.reason)

    def test_recheck_blocks_changes_after_selection(self):
        row = self.inspect()
        (self.path / "new.txt").write_text("unsaved work")
        self.quiet()
        with self.assertRaises(RuntimeError):
            hogs.remove_worktree(row, 30)
        self.assertTrue(self.path.exists())

    def test_discovery_deduplicates_and_respects_scope(self):
        self.quiet()
        rows, warnings = hogs.gather_worktrees(self.root, 30)
        self.assertEqual([r.path for r in rows], [self.path])
        self.assertEqual(warnings, [])
        self.assertEqual(hogs.gather_worktrees(self.repo, 30)[0], [])

    def test_missing_default_branch_is_inconclusive(self):
        self.git(self.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        self.assertTrue(self.inspect().eligible, "origin/main local serve de principal")
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertIn("desconhecida", self.inspect().reason)

    def merge_into_main(self, squash):
        (self.path / "file.txt").write_text("feature work\n")
        self.git(self.path, "commit", "-am", "work")
        (self.path / "more.txt").write_text("more\n")
        self.git(self.path, "add", "more.txt")
        self.git(self.path, "commit", "-m", "more work")
        self.git(self.repo, "commit", "--allow-empty", "-m", "outro trabalho na main")
        if squash:
            self.git(self.repo, "merge", "--squash", "feature")
            self.git(self.repo, "commit", "-m", "squash feature")
        else:
            self.git(self.repo, "cherry-pick", "main..feature")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        self.age()

    def test_squash_merge_is_recognized(self):
        self.merge_into_main(squash=True)
        row = self.inspect()
        self.assertTrue(row.eligible, row.reason)
        self.assertTrue(row.reason.startswith("squash em origin/main"))

    def test_rebase_merge_is_recognized(self):
        self.merge_into_main(squash=False)
        row = self.inspect()
        self.assertTrue(row.eligible, row.reason)
        self.assertTrue(row.reason.startswith("rebase em origin/main"))

    def test_squash_missing_part_of_branch_is_preserved(self):
        (self.path / "file.txt").write_text("feature work\n")
        self.git(self.path, "commit", "-am", "work")
        self.git(self.repo, "merge", "--squash", "feature")
        self.git(self.repo, "commit", "-m", "squash feature")
        (self.path / "later.txt").write_text("commit depois do squash\n")
        self.git(self.path, "add", "later.txt")
        self.git(self.path, "commit", "-m", "later")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        self.age()
        self.assertIn("merge não comprovado", self.inspect().reason)

    def test_regenerable_ignored_dirs_allow_removal(self):
        (self.repo / ".gitignore").write_text(".env\nnode_modules/\ntarget/\nnotes/\n")
        self.git(self.repo, "commit", "-am", "ignore")
        self.git(self.repo, "update-ref", "refs/remotes/origin/main", "main")
        self.git(self.path, "merge", "--ff-only", "main")
        for d in ("node_modules/pkg", "web/node_modules/x", "target/classes"):
            (self.path / d).mkdir(parents=True)
            (self.path / d / "f").write_text("x")
        self.age()
        row = self.inspect()
        self.assertTrue(row.eligible, row.reason)
        (self.path / "notes").mkdir()
        (self.path / "notes" / "plano.md").write_text("plano de outra sessão")
        self.age()
        self.assertIn("notes/", self.inspect().reason)
        (self.path / "notes" / "plano.md").unlink()
        (self.path / "notes").rmdir()
        self.age()
        self.quiet()
        hogs.remove_worktree(self.inspect(), 30)
        self.assertFalse(self.path.exists())

    def test_agent_sessions_are_information_not_blockers(self):
        claude = hogs.claude_key(self.path)
        for folders in ([("Claude Code", claude)], [("Claude Code", claude + "--claude-worktrees-x")],
                        [("Codex", str(self.path / "sub"))]):
            row = self.inspect(agents=hogs.Agents(folders=folders))
            self.assertTrue(row.eligible, row.reason)
            self.assertIn("aberto na pasta", row.users[0])
        parent = [("Claude Code", hogs.claude_key(self.root)), ("Codex", str(self.root))]
        self.assertEqual(self.inspect(agents=hogs.Agents(folders=parent)).users, [])
        cited = hogs.Agents(mentions={self.path: {"Claude Code a", "Claude Code b", "Codex"}})
        row = self.inspect(agents=cited)
        self.assertTrue(row.eligible)
        self.assertEqual(row.users, ["Claude Code a", "Claude Code b", "Codex (últimos 7 d)"])
        row = self.inspect(agents=hogs.Agents(error="falhou"))
        self.assertTrue(row.eligible)
        self.assertIn("sessões: falhou", row.users)

    def fake_home(self):
        home = self.root / "home"
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        patcher = mock.patch.object(hogs, "HOME", home)
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def live_session(self, home, text, sid="s1", pid=None, started=0):
        proj = home / ".claude" / "projects" / "-apps"
        proj.mkdir(exist_ok=True)
        (proj / f"{sid}.jsonl").write_text(text)
        (home / ".claude" / "sessions" / f"{sid}.json").write_text(
            '{"pid": %d, "sessionId": "%s", "cwd": "/apps", "startedAt": %d, "name": "apps-1"}'
            % (pid or os.getpid(), sid, started))
        os.utime(proj / f"{sid}.jsonl", (0, 0))  # transcrição antiga não importa: o processo está vivo
        return proj

    def test_live_claude_session_citing_worktree_is_named(self):
        home = self.fake_home()
        other = self.root / "feature with spaces-2"
        self.live_session(home, json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "input": {"file_path": f"{self.path}/file.txt"}},
            {"type": "text", "text": f"veja {other}."}]}}) + "\n")
        agents = hogs.agent_activity(30, [self.path, other])
        self.assertEqual(agents.error, "")
        self.assertEqual(agents.mentions, {p: {"Claude Code apps-1"} for p in (self.path, other)})
        row = self.inspect(agents=agents)
        self.assertTrue(row.eligible)
        self.assertEqual(row.sessions, ["apps-1"])

    def test_live_claude_subagents_count_but_other_sessions_in_same_folder_do_not(self):
        home = self.fake_home()
        proj = self.live_session(home, "{}\n")
        (proj / "outra-sessao.jsonl").write_text('"cd %s && ls"\n' % self.path)
        self.assertEqual(hogs.agent_activity(30, [self.path]).mentions, {})
        (proj / "s1" / "subagents").mkdir(parents=True)
        (proj / "s1" / "subagents" / "a.jsonl").write_text('"cd %s && ls"\n' % self.path)
        self.assertIn(self.path, hogs.agent_activity(30, [self.path]).mentions)

    def test_only_work_counts_not_tool_output(self):
        home = self.fake_home()
        listing = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": f"{self.path}  abc123 [feature]"}]}}
        context = {"type": "attachment", "attachment": {"content": str(self.path)}}
        self.live_session(home, json.dumps(listing) + "\n" + json.dumps(context) + "\n")
        self.assertEqual(hogs.agent_activity(30, [self.path]).mentions, {})
        typed = {"type": "user", "message": {"content": f"trabalhe em {self.path}"}}
        self.live_session(home, json.dumps(listing) + "\n" + json.dumps(typed) + "\n")
        self.assertIn(self.path, hogs.agent_activity(30, [self.path]).mentions)
        command = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "input": {"command": f"git -C '{self.path}' status"}}]}}
        self.live_session(home, json.dumps(command) + "\n")
        self.assertIn(self.path, hogs.agent_activity(30, [self.path]).mentions)

    def test_dead_or_unrelated_sessions_do_not_block(self):
        home = self.fake_home()
        dead = 2 ** 22 + 12345
        while hogs.pid_alive(dead):
            dead += 1
        self.live_session(home, '"%s"\n' % self.path, pid=dead)
        self.live_session(home, '"%s-outro/x" "%s.bak"\n' % (self.path, self.path), sid="s2")
        agents = hogs.agent_activity(30, [self.path])
        self.assertEqual((agents.error, agents.mentions), ("", {}))

    def test_live_session_without_transcript_is_inconclusive(self):
        home = self.fake_home()
        (home / ".claude" / "sessions" / "x.json").write_text('{"pid": %d, "sessionId": "nao-existe"}' % os.getpid())
        self.assertIn("sem transcrição", hogs.agent_activity(30, [self.path]).error)

    def test_codex_recent_sessions_count(self):
        home = self.fake_home()
        codex = home / ".codex" / "sessions" / "2026" / "11" / "02"
        codex.mkdir(parents=True)
        output = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "output": str(self.path)}}
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "input": f"git -C {self.path} status"}}
        (codex / "rollout-1.jsonl").write_text('{"payload": {"cwd": "/apps"}}\n' + json.dumps(output) + "\n")
        self.assertEqual(hogs.agent_activity(30, [self.path]).mentions, {}, "saída de comando não conta")
        (codex / "rollout-1.jsonl").write_text('{"payload": {"cwd": "/apps"}}\n' + json.dumps(call) + "\n")
        agents = hogs.agent_activity(30, [self.path])
        self.assertIn(("Codex", "/apps"), agents.folders)
        self.assertEqual(agents.mentions, {self.path: {"Codex"}})
        os.utime(codex / "rollout-1.jsonl", (0, 0))
        self.assertEqual(hogs.agent_activity(30, [self.path]).mentions, {})


    def console(self):
        out = io.StringIO()
        return hogs.Console(file=out, width=160, force_terminal=False), out

    def test_selection_numbers(self):
        self.assertEqual(hogs.selection_numbers("a", 3), {1, 2, 3})
        self.assertEqual(hogs.selection_numbers("1, 3-9", 4), {1, 3, 4})
        self.assertEqual(hogs.selection_numbers("", 3), set())
        self.assertEqual(hogs.selection_numbers("0 7 x", 3), set())

    def test_report_numbers_safe_and_summarizes(self):
        row = self.inspect()
        con, out = self.console()
        hogs.worktrees_report(con, [row], self.root, 30)
        text = out.getvalue()
        self.assertIn("seguro · #1", text)
        self.assertIn("SEGURO PARA APAGAR", text)
        self.assertIn("--clean", text)

    def test_clean_offers_only_safe_confirms_and_keeps_branch(self):
        self.quiet()
        safe = self.inspect()
        unsafe = hogs.Worktree(self.root / "x", self.repo, blockers=["merge não comprovado em origin/main"])
        con, out = self.console()
        with mock.patch.object(hogs.Prompt, "ask", return_value="a"), \
                mock.patch.object(hogs.Confirm, "ask", return_value=False):
            hogs.clean_worktrees(con, [safe, unsafe], 30, self.root)
        self.assertTrue(self.path.exists(), "sem confirmação nada sai")
        with mock.patch.object(hogs.Prompt, "ask", return_value="a"), \
                mock.patch.object(hogs.Confirm, "ask", return_value=True):
            hogs.clean_worktrees(con, [safe, unsafe], 30, self.root)
        self.assertFalse(self.path.exists())
        self.assertIn("1 worktree removido", out.getvalue())
        self.assertEqual(self.git(self.repo, "rev-parse", "feature"), safe.head)

    def test_clean_without_safe_offers_nothing(self):
        con, out = self.console()
        with mock.patch.object(hogs.Prompt, "ask") as ask:
            hogs.clean_worktrees(con, [hogs.Worktree(self.path, self.repo, blockers=["x"])], 30, self.root)
        ask.assert_not_called()
        self.assertIn("Nenhum worktree", out.getvalue())

if __name__ == "__main__":
    unittest.main()
