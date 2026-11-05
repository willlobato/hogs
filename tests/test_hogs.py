import io
import sys
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hogs  # noqa: E402


def setUpModule():
    tmp = Path(tempfile.mkdtemp())
    for name, value in (("DATA_DIR", tmp), ("CONFIG", tmp / "config.json"), ("SEEN_TAGS", tmp / "seen_tags.json"),
                        ("MOUNTS_FILE", tmp / "mounts.json"), ("CONVERSATIONS_CACHE", tmp / "conversations.json"),
                        ("STATE", tmp / "state.json"), ("HISTORY", tmp / "history.jsonl"), ("HOME", tmp / "home")):
        if hasattr(hogs, name):
            p = mock.patch.object(hogs, name, value)
            p.start()
            unittest.addModuleCleanup(p.stop)


class SettingTest(unittest.TestCase):
    def test_defaults_and_valid_overrides(self):
        with mock.patch.object(hogs, "load_config", return_value={}):
            self.assertEqual(hogs.setting("rollback_days"), 2)
            self.assertEqual(hogs.setting("stale_worktree_days"), 30)
            self.assertIn("antes", hogs.setting("rollback_tags"))
        with mock.patch.object(hogs, "load_config", return_value={"rollback_days": 5, "rollback_tags": ["old"]}):
            self.assertEqual(hogs.setting("rollback_days"), 5)
            self.assertEqual(hogs.setting("rollback_tags"), ["old"])

    def test_invalid_values_fall_back_to_default(self):
        for bad in ({"rollback_days": "5"}, {"rollback_days": 0}, {"rollback_days": -1}, {"rollback_tags": "antes"}):
            with mock.patch.object(hogs, "load_config", return_value=bad):
                self.assertEqual(hogs.setting(next(iter(bad))), hogs.DEFAULTS[next(iter(bad))])


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


class ToolCacheTest(unittest.TestCase):
    def test_only_big_cache_children_outside_mounts(self):
        c = hogs.CACHES
        sizes = {c / "ferramenta-x": 2_800_000_000, c / "pequeno": 10_000_000, hogs.HOME / "outra": 9e9,
                 c / "montado": 3e9}
        items = hogs.cache_items(sizes, [c / "montado"])
        self.assertEqual([(i.label, i.risk) for i in items], [("Cache: ferramenta-x", hogs.REDOWN)])

    def _gather(self, mounts, known):
        import io
        from unittest import mock
        from rich.console import Console
        big = hogs.CACHES / "ferramenta-x"
        other = hogs.HOME / "pasta-grande"
        sizes = {big: 2_800_000_000, other: 900_000_000}
        with mock.patch.object(hogs, "gather_docker", return_value=hogs.Docker()), \
                mock.patch.object(hogs, "scan_candidates", return_value=[big, other]), \
                mock.patch.object(hogs, "du", side_effect=lambda p: sizes.get(p, 0)), \
                mock.patch.object(hogs, "gather_apps", return_value=[]), \
                mock.patch.object(hogs, "known_mounts", return_value=(mounts, known)), \
                mock.patch.object(hogs, "under_mount", return_value=False), \
                mock.patch.object(hogs, "gather_system", return_value=None), \
                mock.patch.object(hogs, "sign_clone_root", return_value=None), \
                mock.patch.object(hogs, "stale_personal", return_value=[]), \
                mock.patch.object(hogs, "load_config", return_value={}):
            snap = hogs.gather(Console(file=io.StringIO()))
        return snap, hogs.short(big)

    def test_gather_mounts_unknown_offers_nothing(self):
        snap, big = self._gather([], False)
        self.assertFalse([i for i in snap.items if i.label.startswith("Cache: ")])
        self.assertIn(big, [n for n, _ in snap.others])

    def test_mounts_unknown_blocks_only_scan_based_blocks_not_path_defs(self):
        # CLAUDE.md: com mounts desconhecidos, só os blocos que acham arquivos por varredura (arquivos pessoais e
        # caches grandes de apps) deixam de sugerir; os itens de PATH_DEFS continuam como sempre.
        snap, _ = self._gather([], False)
        labels = {i.label for i in snap.items}
        self.assertTrue({pd.label for pd in hogs.PATH_DEFS} <= labels)
        self.assertFalse([i for i in snap.items if i.label.startswith(("Cache: ", "Parado: "))])
        claude_md = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()
        self.assertFalse("no file suggestions at all" in claude_md, "CLAUDE.md promete mais do que o código faz")

    def test_gather_mounts_known_moves_cache_out_of_others(self):
        snap, big = self._gather([], True)
        self.assertEqual([i.label for i in snap.items if i.label.startswith("Cache: ")], ["Cache: ferramenta-x"])
        self.assertNotIn(big, [n for n, _ in snap.others])
        self.assertEqual(len(snap.others), 1)


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


class PersonalTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.old = time.time() - 400 * 86400

    def file(self, rel, blocks):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        os.utime(p, (self.old, self.old))
        return p, blocks

    def test_only_big_old_allocated_files_outside_packages_repos_and_mounts(self):
        files = dict([self.file("video.mov", 2_000_000), self.file("nuvem.zip", 0),
                      self.file("Fotos.photoslibrary/db", 2_000_000), self.file("proj/.git/HEAD", 1),
                      self.file("proj/dump.bak", 2_000_000), self.file("db/data.mdf", 2_000_000),
                      self.file("novo.iso", 2_000_000)])
        os.utime(self.root / "novo.iso", None)
        real_stat = Path.lstat
        def lstat(p):
            st = real_stat(p)
            if p in files:
                return mock.Mock(st_blocks=files[p], st_mtime=st.st_mtime, st_mode=st.st_mode,
                                 st_birthtime=self.old, st_ctime=self.old)
            return st
        with mock.patch.object(hogs.Path, "lstat", lstat), \
                mock.patch.object(hogs, "spotlight_last_used", return_value=None):
            found = hogs.stale_personal([self.root], [self.root / "db"], time.time())
        self.assertEqual([p.name for p, _, _ in found], ["video.mov"])
        self.assertEqual(found[0][1], 2_000_000 * 512)

    def walk(self, mounts=(), spotlight=None, over=None):
        """Roda o walker no tempdir; arquivos regulares viram 2 GB alocados, com datas antigas salvo `over`."""
        real = Path.lstat
        def lstat(p):
            st = real(p)
            if not stat.S_ISREG(st.st_mode):
                return st
            f = dict(st_blocks=4_000_000, st_mtime=self.old, st_birthtime=self.old, st_ctime=self.old,
                     st_mode=st.st_mode)
            f.update((over or {}).get(p.name, {}))
            return mock.Mock(**f)
        with mock.patch.object(hogs.Path, "lstat", lstat), \
                mock.patch.object(hogs, "spotlight_last_used", return_value=spotlight):
            return [p.relative_to(self.root).as_posix()
                    for p, _, _ in hogs.stale_personal([self.root], list(mounts), time.time())]

    def test_recent_birthtime_or_ctime_excludes_copied_files(self):
        self.file("copiado.mov", 0)
        self.assertEqual(self.walk(over={"copiado.mov": {"st_birthtime": time.time()}}), [])
        self.assertEqual(self.walk(over={"copiado.mov": {"st_ctime": time.time()}}), [])
        self.assertEqual(self.walk(), ["copiado.mov"])

    def test_recent_spotlight_use_excludes_file(self):
        self.file("a.mov", 0)
        self.assertEqual(self.walk(spotlight=time.time()), [])
        self.assertEqual(self.walk(spotlight=self.old), ["a.mov"])

    def test_spotlight_last_used_parsing(self):
        def run(out, rc=0):
            return lambda cmd, timeout=60: subprocess.CompletedProcess(cmd, rc, out, "")
        p = Path("/tmp/x")
        with mock.patch.object(hogs, "sh", run("(null)\n")):
            self.assertIsNone(hogs.spotlight_last_used(p))
        with mock.patch.object(hogs, "sh", run("2025-01-02 03:04:05 +0000")):
            self.assertEqual(hogs.spotlight_last_used(p), 1735787045.0)
        with mock.patch.object(hogs, "sh", run("2025-01-02 03:04:05 +0000", 1)):
            self.assertIsNone(hogs.spotlight_last_used(p))
        with mock.patch.object(hogs, "sh", lambda cmd, timeout=60: None):
            self.assertIsNone(hogs.spotlight_last_used(p))

    def test_bind_mounted_file_and_dir_containing_mount(self):
        self.file("cont/db.img", 0)
        self.file("cont/outro.mov", 0)
        self.file("solto.mov", 0)
        got = self.walk(mounts=[self.root / "cont" / "db.img"])
        self.assertEqual(sorted(got), ["cont/outro.mov", "solto.mov"])
        # pasta que apenas contém um mount continua sendo percorrida
        got = self.walk(mounts=[self.root / "cont" / "sub"])
        self.assertEqual(sorted(got), ["cont/db.img", "cont/outro.mov", "solto.mov"])

    def test_symlinks_not_followed(self):
        self.file("real/grande.mov", 0)
        (self.root / "link.mov").symlink_to(self.root / "real" / "grande.mov")
        (self.root / "linkdir").symlink_to(self.root / "real")
        self.assertEqual(self.walk(), ["real/grande.mov"])

    def test_unreadable_dir_skipped(self):
        self.file("ok.mov", 0)
        self.file("fechada/x.mov", 0)
        d = self.root / "fechada"
        d.chmod(0)
        try:
            self.assertEqual(self.walk(), ["ok.mov"])
        finally:
            d.chmod(0o755)

    def test_packages_skipped_case_insensitive(self):
        for pkg in ("A.app", "B.APP", "C.logicx", "D.Photoslibrary", "E.utm"):
            self.file(pkg + "/Contents/big.bin", 0)
        self.file("normal/x.mov", 0)
        self.assertEqual(self.walk(), ["normal/x.mov"])

    def test_trash_passes_path_as_argument_never_inside_script(self):
        calls = []
        ok = lambda cmd, timeout=60: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
        weird = Path('/tmp/a "b" ção.mov')
        with mock.patch.object(hogs, "sh", ok):
            hogs.move_to_trash(weird)
        self.assertEqual(calls[0][-1], str(weird))
        self.assertTrue(all(str(weird) not in part for part in calls[0][:-1]))

    def test_personal_items_are_eval_never_safe(self):
        items = hogs.personal_items([(Path("/tmp/x.mov"), 2e9, time.time() - 200 * 86400)], time.time())
        self.assertIs(items[0].risk, hogs.EVAL)
        self.assertIn("Lixeira", items[0].hint)


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


class RollbackTest(unittest.TestCase):
    W = ["antes", "backup"]

    def test_base_of_rollback_tags(self):
        self.assertEqual(hogs.rollback_base("antes-20261104-2004", self.W), "latest")
        self.assertEqual(hogs.rollback_base("web-local-antes-checkout", self.W), "web-local")
        self.assertEqual(hogs.rollback_base("Backup-1", self.W), "latest")
        self.assertIsNone(hogs.rollback_base("latest", self.W))
        self.assertIsNone(hogs.rollback_base("antesx", self.W))

    def test_tag_date(self):
        self.assertEqual(hogs.tag_date("antes-20261104-2004"), time.mktime((2026, 11, 4, 20, 4, 0, 0, 0, -1)))
        self.assertIsNone(hogs.tag_date("antes-tema-qa"))

    def test_age_uses_youngest_signal(self):
        now = time.time()
        img = {"tag": "antes-20200101-0000"}
        self.assertAlmostEqual(hogs.rollback_age_days(img, {"created_at": now - 3 * 86400}, now - 10 * 86400, now), 3, 1)
        self.assertAlmostEqual(hogs.rollback_age_days(img, {"created_at": None}, now - 1 * 86400, now), 1, 1)

    def test_first_seen_corrupt_or_future_is_now(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "SEEN_TAGS", tmp / "s.json"), mock.patch.object(hogs, "DATA_DIR", tmp):
            (tmp / "s.json").write_text("{lixo")
            self.assertEqual(hogs.first_seen(["a:antes-1"], 100.0), {"a:antes-1": 100.0})
            (tmp / "s.json").write_text(json.dumps({"a:antes-1": 999.0, "sumiu:x": 5.0}))
            self.assertEqual(hogs.first_seen(["a:antes-1"], 100.0), {"a:antes-1": 100.0})

    def items(self, ages_days, current=True):
        now = time.time()
        d = hogs.Docker(running=True)
        d.images = [{"name": "app:antes-x", "repo": "app", "tag": "antes-x", "ref": "app:antes-x", "size": 7e8,
                     "used": False, "origin": "local", "created": "", "id": "1", "created_at": now - 90 * 86400}]
        if current:
            d.images.append({"name": "app:latest", "repo": "app", "tag": "latest", "ref": "app:latest", "size": 7e8,
                             "used": True, "origin": "local", "created": "", "id": "2",
                             "created_at": now - ages_days * 86400})
        with mock.patch.object(hogs, "first_seen", return_value={"app:antes-x": now - 90 * 86400}), \
                mock.patch.object(hogs, "load_config", return_value={}):
            return {i.label: i for i in hogs.docker_clean_items(d)}

    def test_old_rollback_is_safe_recent_is_eval_and_never_grouped(self):
        item = self.items(3)["Ponto de volta app:antes-x"]
        self.assertIs(item.risk, hogs.SAFE)
        self.assertIs(self.items(1)["Ponto de volta app:antes-x"].risk, hogs.EVAL)
        calls = []
        with mock.patch.object(hogs, "sh", lambda cmd, timeout=60: calls.append(cmd) or
                               subprocess.CompletedProcess(cmd, 0, "", "")):
            item.action()
        self.assertEqual(calls, [["docker", "rmi", "app:antes-x"]])  # só a tag, nunca o ID

    def test_without_current_image_it_stays_a_local_loss(self):
        labels = self.items(3, current=False)
        self.assertNotIn("Ponto de volta app:antes-x", labels)
        self.assertIs(labels["Imagem app:antes-x"].risk, hogs.LOSS)


    def build(self, rollback_extra=None, current_extra=None, tag="antes-x"):
        now = time.time()
        d = hogs.Docker(running=True)
        roll = {"name": f"app:{tag}", "repo": "app", "tag": tag, "ref": f"app:{tag}", "size": 7e8, "used": False,
                "origin": "local", "created": "", "id": "1", "created_at": now - 90 * 86400}
        cur = {"name": "app:latest", "repo": "app", "tag": "latest", "ref": "app:latest", "size": 7e8,
               "used": True, "origin": "local", "created": "", "id": "2", "created_at": now - 90 * 86400}
        roll.update(rollback_extra or {})
        cur.update(current_extra or {})
        d.images = [roll, cur]
        return d, now

    def run_items(self, d, seen):
        with mock.patch.object(hogs, "first_seen", return_value=seen), \
                mock.patch.object(hogs, "load_config", return_value={}):
            return {i.label: i for i in hogs.docker_clean_items(d)}

    def test_no_reliable_old_signal_is_eval(self):
        d, now = self.build(current_extra={"created_at": None})
        item = self.run_items(d, {"app:antes-x": now})["Ponto de volta app:antes-x"]
        self.assertIs(item.risk, hogs.EVAL)

    def test_recent_date_in_tag_name_drives_eval(self):
        tag = "antes-" + time.strftime("%Y%m%d") + "-0000"
        d, now = self.build(tag=tag)
        item = self.run_items(d, {f"app:{tag}": now - 90 * 86400})[f"Ponto de volta app:{tag}"]
        self.assertIs(item.risk, hogs.EVAL)

    def test_first_seen_not_called_without_rollback_candidates(self):
        d = hogs.Docker(running=True)
        d.images = [{"name": "app:v1", "repo": "app", "tag": "v1", "ref": "app:v1", "size": 7e8, "used": False,
                     "origin": "local", "created": "", "id": "1"}]
        with mock.patch.object(hogs, "first_seen") as fs, mock.patch.object(hogs, "load_config", return_value={}):
            hogs.docker_clean_items(d)
        fs.assert_not_called()

    def test_small_hub_rollback_is_not_repeated_in_small_images_group(self):
        d, now = self.build(rollback_extra={"origin": "hub", "size": 1e8}, current_extra={"origin": "hub"})
        labels = self.run_items(d, {"app:antes-x": now - 90 * 86400})
        self.assertIn("Ponto de volta app:antes-x", labels)
        self.assertFalse([k for k in labels if k.startswith("Imagens pequenas")])

    def test_same_image_as_current_frees_nothing(self):
        d, now = self.build(rollback_extra={"id": "2"})
        item = self.run_items(d, {"app:antes-x": now - 90 * 86400})["Ponto de volta app:antes-x"]
        self.assertEqual(item.size, 0)
        self.assertIn("só remove a tag", item.hint)

    def test_parse_epoch(self):
        self.assertEqual(hogs.parse_epoch("2026-11-04 20:04:12 -0300 -03"),
                         datetime(2026, 11, 4, 23, 4, 12, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(hogs.parse_epoch("lixo"))
        self.assertIsNone(hogs.parse_epoch(""))

    def test_gather_docker_reads_created_at(self):
        img = {"Repository": "app", "Tag": "latest", "ID": "sha256:" + "a" * 64, "UniqueSize": "1GB",
               "Containers": "0", "CreatedSince": "1 hour ago", "CreatedAt": "2026-11-04 20:04:12 -0300 -03"}
        sh, _ = DockerCleanItemsTest.fake_docker(images=[img])
        with mock.patch.object(hogs, "sh", sh):
            d = hogs.gather_docker()
        self.assertEqual(d.images[0]["created_at"], hogs.parse_epoch("2026-11-04 20:04:12 -0300 -03"))


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

    @staticmethod
    def gather_with(mounts_reply, state="exited", containers=True, volumes_reply=None, calls=None):
        """gather_docker com `docker inspect` de mounts respondendo `mounts_reply` (None = sh falhou) e
        `docker volume inspect` respondendo `volumes_reply`."""
        cs = [{"ID": "a" * 64, "Names": "db", "State": state, "Labels": "", "Size": "1MB"}] if containers else []
        sh, _ = DockerCleanItemsTest.fake_docker(containers=cs, inspect=None)

        def sh2(cmd, timeout=60):
            if calls is not None:
                calls.append(cmd)
            if cmd[:3] == ["docker", "volume", "inspect"]:
                return volumes_reply
            if cmd[:2] == ["docker", "inspect"]:
                if "Mounts" in cmd[3]:
                    return mounts_reply(cmd) if callable(mounts_reply) else mounts_reply
                return subprocess.CompletedProcess(cmd, 0, f"{'a' * 64}|2020-01-01T00:00:00Z|2019-01-01T00:00:00Z\n", "")
            return sh(cmd, timeout)
        with mock.patch.object(hogs, "sh", sh2):
            return hogs.gather_docker()

    @staticmethod
    def ok(out):
        return subprocess.CompletedProcess([], 0, out, "")

    BINDS = json.dumps([{"Type": "bind", "Source": "/tmp/dados-db"}, {"Type": "volume", "Source": "/var/lib/x"}]) + "\n"

    def test_gather_reads_bind_mounts_of_stopped_containers_too(self):
        d = self.gather_with(self.ok(self.BINDS))
        self.assertTrue(d.mounts_ok)
        self.assertEqual(d.mounts, [Path("/tmp/dados-db").resolve()])

    def test_gather_reads_bind_mounts_of_running_containers(self):
        d = self.gather_with(self.ok(self.BINDS), state="running")
        self.assertTrue(d.mounts_ok)
        self.assertEqual(d.mounts, [Path("/tmp/dados-db").resolve()])

    def test_gather_mounts_unknown_when_inspect_fails(self):
        for reply in (None, subprocess.CompletedProcess([], 1, "", "erro"), self.ok("não é json\n")):
            d = self.gather_with(reply)
            self.assertFalse(d.mounts_ok)
            self.assertEqual(d.mounts, [])

    def test_gather_mounts_unknown_when_resolve_raises(self):
        with mock.patch.object(Path, "resolve", side_effect=OSError("x")):
            d = self.gather_with(self.ok(self.BINDS))
        self.assertFalse(d.mounts_ok)

    def test_gather_without_containers_is_ok_and_empty(self):
        d = self.gather_with(None, containers=False)
        self.assertTrue(d.mounts_ok)
        self.assertEqual(d.mounts, [])

    # Volume nomeado com driver_opts de bind (`--opt type=none --opt o=bind --opt device=/pasta`): o mount do
    # container é `volume`, mas os dados ficam na pasta do host.
    VOLUMES = json.dumps([{"Type": "volume", "Name": "pgdata", "Source": "/var/lib/docker/volumes/pgdata/_data"},
                          {"Type": "volume", "Name": "cache", "Source": "/var/lib/docker/volumes/cache/_data"},
                          {"Type": "volume", "Name": "pgdata", "Source": "/var/lib/docker/volumes/pgdata/_data"}]) + "\n"

    def test_bind_backed_named_volume_protects_its_host_folder(self):
        host = Path(tempfile.mkdtemp())
        calls = []
        options = json.dumps({"device": str(host), "o": "bind", "type": "none"}) + "\nnull\n"
        d = self.gather_with(self.ok(self.VOLUMES), volumes_reply=self.ok(options), calls=calls)
        self.assertTrue(d.mounts_ok)
        self.assertEqual(d.mounts, [host.resolve()])
        volume_calls = [c for c in calls if c[:3] == ["docker", "volume", "inspect"]]
        self.assertEqual(len(volume_calls), 1)   # uma chamada só, cada volume uma vez
        self.assertEqual(volume_calls[0][-2:], ["pgdata", "cache"])
        self.assertTrue(hogs.under_mount(host / "base.mdf", d.mounts))

    def test_rbind_with_options_also_protects(self):
        host = Path(tempfile.mkdtemp())
        options = json.dumps({"device": str(host), "o": "rbind,ro", "type": "none"}) + "\nnull\n"
        d = self.gather_with(self.ok(self.VOLUMES), volumes_reply=self.ok(options))
        self.assertEqual(d.mounts, [host.resolve()])

    def test_volume_options_unknown_means_mounts_unknown(self):
        for reply in (None, subprocess.CompletedProcess([], 1, "", "Error: no such volume"), self.ok("lixo\n"),
                      self.ok("null\n")):   # resposta com menos linhas que volumes
            d = self.gather_with(self.ok(self.VOLUMES), volumes_reply=reply)
            self.assertFalse(d.mounts_ok, reply)
            self.assertEqual(d.mounts, [])

    def test_under_mount_resolves_the_path(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / "real").mkdir()
        (tmp / "link").symlink_to(tmp / "real")
        self.assertTrue(hogs.under_mount(tmp / "link" / "x.mdf", [tmp / "real"]))
        self.assertTrue(hogs.under_mount(tmp / "link", [tmp / "real"]))

    def test_corrupt_saved_mounts_count_as_nothing_saved(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "MOUNTS_FILE", tmp / "m.json"), \
                mock.patch.object(hogs.shutil, "which", return_value="/usr/bin/docker"):
            for body in ('{"mounts": [1, 2]}', '{"mounts": "x"}', '[1]', 'lixo', '{"mounts": [""]}'):
                (tmp / "m.json").write_text(body)
                self.assertEqual(hogs.known_mounts(hogs.Docker()), ([], False), body)

    def test_known_mounts_falls_back_to_saved_then_unknown(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "MOUNTS_FILE", tmp / "m.json"), \
                mock.patch.object(hogs.shutil, "which", return_value="/usr/bin/docker"):
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([], False))
            hogs.known_mounts(hogs.Docker(running=True, mounts=[Path("/x/db")], mounts_ok=True))
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([Path("/x/db")], True))

    def test_no_docker_installed_means_no_mounts_known_ok(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(hogs, "MOUNTS_FILE", tmp / "m.json"), mock.patch.object(hogs.shutil, "which", return_value=None):
            self.assertEqual(hogs.known_mounts(hogs.Docker()), ([], True))


class OciRollbackTest(unittest.TestCase):
    SRC = "https://github.com/acme/shop-api"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "shop-api"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True,
                                      text=True).stdout.strip()
        for args in (("init", "-b", "main"), ("config", "user.name", "T"), ("config", "user.email", "t@example.invalid"),
                     ("config", "commit.gpgsign", "false"), ("remote", "add", "origin", "git@github.com:Acme/shop-api.git"),
                     ("commit", "--allow-empty", "-m", "a")):
            g(*args)
        self.on_main = g("rev-parse", "HEAD")
        g("update-ref", "refs/remotes/origin/main", "HEAD")
        g("commit", "--allow-empty", "-m", "b")
        self.off_main = g("rev-parse", "HEAD")

    def docker(self, labels):
        now = time.time()
        d = hogs.Docker(running=True)
        d.images = [{"name": "shop-api:antes-x", "repo": "shop-api", "tag": "antes-x", "ref": "shop-api:antes-x",
                     "size": 7e8, "used": False, "origin": "local", "created": "", "id": "1", "created_at": now},
                    {"name": "shop-api:latest", "repo": "shop-api", "tag": "latest", "ref": "shop-api:latest",
                     "size": 7e8, "used": True, "origin": "local", "created": "", "id": "2", "created_at": now,
                     "labels": labels}]
        return d

    def released(self, d, repos="default"):
        with mock.patch.object(hogs, "load_config", return_value={}):
            return hogs.released_images(d, [self.repo] if repos == "default" else repos)

    def test_normalize_remote(self):
        for url in ("git@github.com:Acme/shop-api.git", "https://github.com/acme/shop-api",
                    "https://github.com/acme/shop-api.git/", "ssh://git@github.com:22/acme/shop-api.git"):
            self.assertEqual(hogs.normalize_remote(url), "github.com/acme/shop-api")

    def test_commit_on_main_makes_recent_rollback_safe(self):
        d = self.docker({hogs.OCI_REVISION: self.on_main, hogs.OCI_SOURCE: self.SRC})
        self.assertEqual(self.released(d), {"shop-api:latest"})
        d.released = {"shop-api:latest"}
        with mock.patch.object(hogs, "first_seen", return_value={"shop-api:antes-x": time.time()}), \
                mock.patch.object(hogs, "load_config", return_value={}):
            item = {i.label: i for i in hogs.docker_clean_items(d)}["Ponto de volta shop-api:antes-x"]
        self.assertIs(item.risk, hogs.SAFE)
        self.assertIn("já está na main", item.hint)

    def test_off_main_unknown_bad_or_other_source_falls_back(self):
        for rev in (self.off_main, "f" * 40, "--output=/tmp/x", ""):
            self.assertEqual(self.released(self.docker({hogs.OCI_REVISION: rev, hogs.OCI_SOURCE: self.SRC})), set(), rev)
        other = {hogs.OCI_REVISION: self.on_main, hogs.OCI_SOURCE: "https://github.com/acme/outro"}
        self.assertEqual(self.released(self.docker(other)), set())

    def test_without_labels_or_repos_no_git_call(self):
        with mock.patch.object(hogs, "git_at") as git:
            self.assertEqual(self.released(self.docker({})), set())
            ok = {hogs.OCI_REVISION: self.on_main, hogs.OCI_SOURCE: self.SRC}
            self.assertEqual(self.released(self.docker(ok), repos=[]), set())
        git.assert_not_called()

    def test_gather_docker_reads_labels(self):
        img = {"Repository": "shop-api", "Tag": "latest", "ID": "sha256:" + "a" * 64, "UniqueSize": "1GB",
               "Containers": "0", "CreatedSince": "1 hour ago", "CreatedAt": ""}
        labels = {hogs.OCI_REVISION: "abc1234", hogs.OCI_SOURCE: self.SRC}
        base, _ = DockerCleanItemsTest.fake_docker(images=[img])
        for reply, expected in ((f"sha256:{'a' * 64}\t{json.dumps(labels)}\n", labels), (None, {})):
            def sh(cmd, timeout=60, reply=reply):
                if cmd[:3] == ["docker", "image", "inspect"]:
                    return reply and subprocess.CompletedProcess(cmd, 0, reply, "")
                return base(cmd, timeout)
            with mock.patch.object(hogs, "sh", sh):
                self.assertEqual(hogs.gather_docker().images[0]["labels"], expected)


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


class SystemTest(unittest.TestCase):
    APFS = plistlib.dumps({"Containers": [{"ContainerReference": "disk3", "Volumes": [
        {"Roles": ["System"], "CapacityInUse": 18_000_000_000}, {"Roles": ["Preboot"], "CapacityInUse": 17_300_000_000},
        {"Roles": ["Data"], "CapacityInUse": 411_000_000_000}, {"Roles": ["VM"], "CapacityInUse": 15_000_000_000},
        {"Roles": [], "CapacityInUse": 1}]}, {"ContainerReference": "disk6", "Volumes": [
        {"Roles": ["Data"], "CapacityInUse": 43_000_000}]}]})

    def test_parse_apfs_only_our_container(self):
        v = hogs.parse_apfs(self.APFS, "disk3")
        self.assertEqual(v["Preboot"], 17_300_000_000)
        self.assertEqual(v["Data"], 411_000_000_000)
        self.assertNotIn("", v)

    def test_parse_swap_and_boot(self):
        self.assertEqual(hogs.parse_swap("total = 15360,00M  used = 13799,12M  free = 1560,88M  (encrypted)"),
                         int(13799.12 * 2**20))
        self.assertEqual(hogs.parse_swap("total = 2048.00M  used = 0.00M  free = 2048.00M"), 0)
        self.assertAlmostEqual(hogs.parse_boottime("{ sec = 1000, usec = 0 } Mon", 1000 + 86 * 86400), 86)

    def test_advice_only_when_it_pays_and_never_an_item(self):
        si = hogs.SystemInfo({"Preboot": 17_300_000_000}, 13_800_000_000, 86, True)
        labels = [a[0] for a in hogs.system_advice(si)]
        self.assertEqual(labels, ["Reiniciar o Mac", "Aplicar a atualização do macOS"])
        self.assertEqual(hogs.system_advice(hogs.SystemInfo({"Preboot": 5e9}, 13_800_000_000, 3, False)), [])


class BreakdownTest(unittest.TestCase):
    def snap(self, raw, dev, others):
        d = hogs.Docker(raw_real=raw)
        items = [hogs.Item("dev", "npm", dev, hogs.SAFE)]
        si = hogs.SystemInfo({"System": 18e9, "Preboot": 17e9, "VM": 15e9, "Data": 400e9}, 0, 1, False)
        return hogs.Snapshot(494e9, 28e9, d, items, [("~/x", others)], system=si)

    def test_parts_sum_to_used_and_unknown_is_never_negative(self):
        for raw, dev, others in ((92e9, 18e9, 60e9), (300e9, 100e9, 200e9)):
            parts = dict((k, v) for k, v, _ in hogs.breakdown(self.snap(raw, dev, others)))
            self.assertGreaterEqual(parts["Não identificado"], 0)
            self.assertAlmostEqual(sum(v for k, v in parts.items() if k != "Livre"), 466e9, delta=1e6)
            self.assertEqual(parts["macOS e swap"], 50e9)
            self.assertEqual(parts["Livre"], 28e9)

    def test_advice_and_eval_never_count_as_recommended_space(self):
        s = self.snap(92e9, 0, 0)
        s.items.append(hogs.Item("apps", "Parado: ~/x.mov", 5e9, hogs.EVAL, lambda: None))
        s.advice = [("Reiniciar o Mac", 13e9, "")]
        con = hogs.Console(file=io.StringIO(), width=140)
        con.print(hogs.header(s))
        self.assertIn("com o recomendado: 28,0 GB livres", con.file.getvalue())

    def test_trim_off_docker_space_is_not_recommended_space(self):
        s = self.snap(92e9, 0, 0)
        cleanable = [hogs.Item("docker", "Docker: cache de build", 10e9, hogs.SAFE, lambda: None),
                     hogs.Item("apps", "Lixeira", 2e9, hogs.SAFE, lambda: None)]
        tmp = Path(tempfile.mkdtemp())

        def header(settings):
            if settings is not None:
                (tmp / "settings.json").write_text(json.dumps(settings))
            con = hogs.Console(file=io.StringIO(), width=220)
            with mock.patch.object(hogs, "DOCKER_SETTINGS", tmp / "settings.json"):
                con.print(hogs.header(s, [], cleanable))
            return " ".join(con.file.getvalue().split())
        note = "(Docker não devolve espaço: TRIM desligado)"
        out = header(None)                       # sem o arquivo: TRIM desconhecido, não é "desligado"
        self.assertIn("com o recomendado: 40,0 GB livres", out)
        self.assertNotIn(note, out)
        out = header({"DiskTRIM": True})
        self.assertIn("com o recomendado: 40,0 GB livres", out)
        self.assertNotIn(note, out)
        out = header({"DiskTRIM": False})
        self.assertIn("com o recomendado: 30,0 GB livres", out)
        self.assertIn(note, out)

    def render_report(self, s):
        con = hogs.Console(file=io.StringIO(), width=160)
        with mock.patch.object(hogs, "load_config", return_value={}):
            hogs.report(con, s, [])
        return con.file.getvalue()

    def test_report_recommends_only_safe_and_lists_what_it_protects(self):
        s = self.snap(92e9, 0, 0)
        s.items += [hogs.Item("apps", "Lixeira", 2e9, hogs.SAFE, lambda: None),
                    hogs.Item("apps", "Parado: ~/a.mov", 5e9, hogs.EVAL, lambda: None, kind="pessoal"),
                    hogs.Item("apps", "Parado: ~/b.iso", 1e9, hogs.EVAL, lambda: None, kind="pessoal"),
                    hogs.Item("docker", "Ponto de volta app:antes-x", 3e9, hogs.EVAL, lambda: None,
                              kind="ponto_de_volta")]
        s.advice = [("Reiniciar o Mac", 13e9, "swap 13,0 GB · ligado há 20 dias")]
        s.docker.live = ["shop-db"]
        s.mounts = [Path("/u/dev/db/data")]
        out = self.render_report(s)
        self.assertIn("com o recomendado: 30,0 GB livres", out)
        recommended = out.split("✨ Recomendado agora")[1].split("🛡 Não mexo")[0]
        self.assertIn("Lixeira", recommended)
        self.assertIn("↻", recommended)
        files = next(line for line in recommended.splitlines() if "Arquivos parados para avaliar (2)" in line)
        self.assertIn("6,0 GB", files)
        rollbacks = next(line for line in recommended.splitlines() if "Pontos de volta recentes (1)" in line)
        self.assertIn("3,0 GB", rollbacks)
        self.assertNotIn("Docker não respondeu", out)
        protected = out.split("🛡 Não mexo")[1]
        self.assertIn("1 container rodando (shop-db)", protected)
        self.assertIn("1 caminho montado em containers (/u/dev/db/data)", protected)

    def test_report_says_why_files_are_not_suggested_without_docker(self):
        s = self.snap(0, 0, 0)
        s.mounts_known = False
        out = " ".join(self.render_report(s).split())
        self.assertIn("Docker não respondeu: não sei quais pastas os containers usam, então não sugiro arquivos.", out)
        protected = out.split("🛡 Não mexo")[1]
        self.assertIn("Docker não respondeu: não sei o que está em uso.", protected)
        self.assertNotIn("Nada em uso pelo Docker agora", protected)

    def test_report_shows_guard_reason_of_open_app(self):
        s = self.snap(0, 0, 0)
        group = hogs.CloneGroup("Navegador", "Navegador", "122.0", {"120.0": [Path("/x/c1")]})
        with mock.patch.object(hogs, "du", return_value=2e9):
            s.items += hogs.sign_clone_items([group])
        with mock.patch.object(hogs, "process_running", return_value=True):
            recommended = self.render_report(s).split("✨ Recomendado agora")[1].split("🛡 Não mexo")[0]
        line = next(line for line in recommended.splitlines() if "Cópias antigas do Navegador" in line)
        self.assertIn("feche o Navegador antes", line)

    def test_rollback_risk_in_report_rows_matches_clean(self):
        tmp = Path(tempfile.mkdtemp())
        now = time.time()
        (tmp / "s.json").write_text(json.dumps({"app:antes-velho": now - 30 * 86400}))

        def img(tag, used=False):
            return {"name": f"app:{tag}", "repo": "app", "tag": tag, "ref": f"app:{tag}", "size": 1e9, "used": used,
                    "origin": "local", "created": "", "id": tag, "created_at": now - 60 * 86400}
        d = hogs.Docker(running=True, images=[img("latest", True), img("antes-velho"), img("antes-novo")])
        with mock.patch.object(hogs, "SEEN_TAGS", tmp / "s.json"), mock.patch.object(hogs, "DATA_DIR", tmp), \
                mock.patch.object(hogs, "load_config", return_value={}):
            clean = {i.label.split()[-1]: i.risk for i in hogs.clean_items(hogs.Snapshot(1, 1, d, [], []))
                     if i.label.startswith("Ponto de volta")}
            rows = hogs.docker_rows(d)
            con = hogs.Console(file=io.StringIO(), width=160)
            con.print(hogs.unused_images_table(d))
        self.assertEqual(clean, {"app:antes-velho": hogs.SAFE, "app:antes-novo": hogs.EVAL})
        for name, risk in clean.items():
            row = next(r for r in rows if r[0] == f"Ponto de volta {name}")
            self.assertIs(row[2], risk)
        self.assertIn(("Imagens sem uso, construídas aqui (0)", 0), [(r[0], r[1]) for r in rows])
        table = con.file.getvalue()
        self.assertNotIn("construída aqui", table)
        self.assertIn(f"{hogs.SAFE.icon} ponto de volta", table)
        self.assertIn(f"{hogs.EVAL.icon} ponto de volta", table)

    def test_each_rollback_point_has_its_own_line_never_hidden_in_a_total(self):
        now = time.time()

        def img(tag, used=False):
            return {"name": f"app:{tag}", "repo": "app", "tag": tag, "ref": f"app:{tag}", "size": 1e9, "used": used,
                    "origin": "local", "created": "", "id": tag, "created_at": now - 60 * 86400}
        tags = ["antes-a", "antes-b", "antes-c", "antes-d", "antes-velho"]
        d = hogs.Docker(running=True, images=[img("latest", True)] + [img(t) for t in tags])
        seen = {f"app:{t}": now - (30 if t == "antes-velho" else 0) * 86400 for t in tags}
        con = hogs.Console(file=io.StringIO(), width=220)
        with mock.patch.object(hogs, "first_seen", return_value=seen), \
                mock.patch.object(hogs, "load_config", return_value={}):
            rows = hogs.docker_rows(d)
            hogs.report(con, hogs.Snapshot(494e9, 28e9, d, [], []), [])
        labels = [r[0] for r in rows if "volta" in r[0]]
        self.assertEqual(sorted(labels), sorted(f"Ponto de volta app:{t}" for t in tags))
        out = con.file.getvalue()
        docker_table = out.split("🐳 Docker")[-1]
        for t in tags:
            self.assertIn(f"Ponto de volta app:{t}", docker_table)
        recommended = " ".join(out.split("✨ Recomendado agora")[1].split("🛡 Não mexo")[0].split())
        self.assertIn("Pontos de volta recentes (4)", recommended)
        self.assertIn("app:antes-a, app:antes-b, app:antes-c, app:antes-d", recommended)
        self.assertNotIn("…", recommended)

    def test_worktrees_folder_is_remembered_keeping_other_settings(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "projetos").mkdir()
        (tmp / "config.json").write_text(json.dumps({"alert_gb": 30}))
        with mock.patch.object(hogs, "DATA_DIR", tmp), mock.patch.object(hogs, "CONFIG", tmp / "config.json"), \
                mock.patch.object(sys, "argv", ["hogs", "--worktrees", str(tmp / "projetos")]), \
                mock.patch.object(hogs, "gather_worktrees", return_value=([], [])), \
                mock.patch.object(hogs, "worktrees_report"):
            hogs.run()
            self.assertEqual(hogs.load_config(),
                             {"alert_gb": 30, "worktrees_root": str((tmp / "projetos").resolve())})
            self.assertEqual(hogs.projects_root(), (tmp / "projetos").resolve())

    def test_history_keys_skip_counts_and_files_to_evaluate(self):
        s = self.snap(0, 0, 0)
        s.items = [hogs.Item("apps", "Cópias antigas do Navegador (3)", 2e9, hogs.SAFE),
                   hogs.Item("apps", "Parado: ~/a.mov", 5e9, hogs.EVAL)]
        s.projects = 7e9
        sizes = hogs.snapshot_sizes(s)
        self.assertIn("Cópias antigas do Navegador", sizes)
        self.assertNotIn("Parado: ~/a.mov", sizes)
        self.assertEqual(sizes["Projetos"], 7e9)

    def _gather(self, root, known, candidates, sizes):
        si = hogs.SystemInfo({"Preboot": 5e9}, 13e9, 30, False)
        stale = mock.Mock(return_value=[])
        with mock.patch.object(hogs, "gather_docker", return_value=hogs.Docker()), \
                mock.patch.object(hogs, "scan_candidates", return_value=candidates), \
                mock.patch.object(hogs, "du", side_effect=lambda p: sizes.get(p, 0)), \
                mock.patch.object(hogs, "gather_apps", return_value=[]), \
                mock.patch.object(hogs, "known_mounts", return_value=([], known)), \
                mock.patch.object(hogs, "gather_system", return_value=si), \
                mock.patch.object(hogs, "sign_clone_root", return_value=None), \
                mock.patch.object(hogs, "stale_personal", stale), \
                mock.patch.object(hogs, "load_config", return_value={"worktrees_root": str(root)}):
            return hogs.gather(hogs.Console(file=io.StringIO())), stale

    def test_gather_counts_projects_once_and_files_only_when_mounts_known(self):
        tmp = Path(tempfile.mkdtemp())
        dev, other = tmp / "dev", tmp / "outra"
        (dev / "wt").mkdir(parents=True)
        other.mkdir()
        sizes = {dev: 50e9, dev / "wt": 20e9, other: 3e9}
        snap, stale = self._gather(dev / "wt", True, [dev, other], sizes)
        self.assertEqual(snap.projects, 20e9)
        self.assertEqual(dict(snap.others)[hogs.short(dev)], 30e9)
        self.assertEqual([a[0] for a in snap.advice], ["Reiniciar o Mac"])
        stale.assert_called_once()
        snap, stale = self._gather(dev, False, [dev, other], sizes)
        self.assertEqual(snap.projects, 50e9)
        self.assertNotIn(hogs.short(dev), dict(snap.others))
        self.assertFalse(snap.mounts_known)
        stale.assert_not_called()
        snap, _ = self._gather(tmp / "nao-existe", True, [dev, other], sizes)
        self.assertEqual(snap.projects, 0)
        self.assertEqual(dict(snap.others)[hogs.short(dev)], 50e9)


class PreventionTest(unittest.TestCase):
    def panel(self, settings, docker=hogs.Docker()):
        home = Path(tempfile.mkdtemp())
        if settings is not None:
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(json.dumps(settings))
        snap = hogs.Snapshot(494e9, 28e9, docker, [], [])
        with mock.patch.object(hogs, "HOME", home), mock.patch.object(hogs, "load_config", return_value={}):
            p = hogs.prevention_panel(snap)
        if settings is not None:
            self.assertEqual(json.loads((home / ".claude" / "settings.json").read_text()), settings)  # nunca altera
        if p is None:
            return None
        con = hogs.Console(file=io.StringIO(), width=200)
        con.print(p)
        return con.file.getvalue()

    def test_recommends_keeping_conversations_only_when_needed(self):
        text = self.panel({})
        self.assertIn("apagadas depois de 30 dias", text)
        self.assertIn('"cleanupPeriodDays": 365', text)
        self.assertIn("apagadas depois de 7 dias", self.panel({"cleanupPeriodDays": 7}))
        text = self.panel({"cleanupPeriodDays": 400})
        self.assertIn("guardadas por 400 dias", text)
        self.assertNotIn("365", text)

    def test_without_claude_code_no_line(self):
        self.assertIsNone(self.panel(None))
        text = self.panel(None, hogs.Docker(running=True))
        self.assertNotIn("Claude", text)


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


class SignCloneTest(unittest.TestCase):
    def make(self, root, clone, app, version, exe="App"):
        c = root / "com.example.App.code_sign_clone" / clone / f"{app}.app.bundle" / "Contents"
        c.mkdir(parents=True)
        (c / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version, "CFBundleExecutable": exe}))
        (c / "blob").write_bytes(b"x" * 4096)

    def tmpdir(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def install(self, base, app, version):
        (base / f"{app}.app/Contents").mkdir(parents=True)
        (base / f"{app}.app/Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": version}))

    def test_unknown_installed_version_offers_nothing(self):
        tmp = self.tmpdir()
        root = tmp / "X"
        self.make(root, "code_sign_clone.a", "Navegador", "155")
        with mock.patch.object(hogs, "APP_GLOBS", [tmp / "Applications", tmp / "home" / "Applications"]):
            self.assertEqual(hogs.sign_clone_items(hogs.gather_sign_clones(root)), [])

    def test_app_found_in_user_applications(self):
        tmp = self.tmpdir()
        root = tmp / "X"
        self.install(tmp / "home" / "Applications", "Navegador", "156")
        self.make(root, "code_sign_clone.a", "Navegador", "155")
        with mock.patch.object(hogs, "APP_GLOBS", [tmp / "Applications", tmp / "home" / "Applications"]), \
                mock.patch.object(hogs, "du", return_value=10):
            items = hogs.sign_clone_items(hogs.gather_sign_clones(root))
        self.assertEqual(len(items), 1)
        self.assertIn("a instalada é 156", items[0].hint)

    def test_only_old_versions_one_copy_counted_and_guarded(self):
        tmp = self.tmpdir()
        apps, root = tmp / "Applications", tmp / "X"
        (apps / "Navegador.app/Contents").mkdir(parents=True)
        (apps / "Navegador.app/Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "156"}))
        for n, v in (("a", "156"), ("b", "156"), ("c", "155"), ("d", "155"), ("e", "152")):
            self.make(root, f"code_sign_clone.{n}", "Navegador", v)
        with mock.patch.object(hogs, "APP_GLOBS", [apps]), \
                mock.patch.object(hogs, "du", side_effect=lambda p: 1_000_000_000):
            items = hogs.sign_clone_items(hogs.gather_sign_clones(root))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.label, item.size, item.risk), ("Cópias antigas do Navegador (3)", 2_000_000_000, hogs.SAFE))
        with mock.patch.object(hogs, "process_running", return_value=True):
            self.assertEqual(item.guard(), "feche o Navegador antes")
        with mock.patch.object(hogs, "process_running", return_value=False):
            self.assertIsNone(item.guard())
        item.action()
        left = sorted(p.name for p in (root / "com.example.App.code_sign_clone").iterdir())
        self.assertEqual(left, ["code_sign_clone.a", "code_sign_clone.b"])


class ProcessRunningTest(unittest.TestCase):
    """A guarda de app aberto nunca diz "fechado" com o app aberto."""

    @staticmethod
    def pgrep(procs):
        """pgrep falso (regex estendida): -x casa o nome do processo inteiro, -f a linha de comando."""
        import re

        def sh(cmd, timeout=60):
            if cmd[0] != "pgrep":
                return subprocess.CompletedProcess(cmd, 0, "", "")
            flag, pattern = cmd[1], cmd[2]
            hit = any(re.fullmatch(pattern, comm) if flag == "-x" else re.search(pattern, args)
                      for comm, args in procs)
            return subprocess.CompletedProcess(cmd, 0 if hit else 1, "123\n" if hit else "", "")
        return sh

    def running(self, name, procs):
        with mock.patch.object(hogs, "sh", self.pgrep(procs)):
            return hogs.process_running(name)

    def test_executable_name_longer_than_the_kernel_name(self):
        exe = "Navegador Super Rapido Helper"          # 29 caracteres; o kernel guarda 16 (MAXCOMLEN)
        app = f"/Applications/Navegador.app/Contents/MacOS/{exe}"
        self.assertTrue(self.running(exe, [(exe[:16], app)]))                 # pelo nome cortado ou pelo caminho
        self.assertTrue(self.running(exe, [(exe[:16], "")]))                  # só o nome cortado
        self.assertTrue(self.running(exe, [("outro-nome", f"{app} --type=main")]))   # só o caminho

    def test_name_with_regex_characters_is_literal(self):
        exe = "Navegador (Beta)+"
        self.assertTrue(self.running(exe, [(exe, f"/Applications/N.app/Contents/MacOS/{exe}")]))

    def test_closed_app_is_not_running(self):
        procs = [("Navegador2", "/Applications/N2.app/Contents/MacOS/Navegador2"), ("bash", "bash -c Navegador")]
        self.assertFalse(self.running("Navegador", procs))


class ConversationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.scope, self.projects, self.cache = base / "dev", base / "projects", base / "conv.json"
        self.wt = self.scope / "shop-api" / "feature with spaces"

    def rec(self, kind, cwd="/x", branch="", ts="2026-10-01T10:00:00.000Z", content="oi"):
        return json.dumps({"type": kind, "cwd": cwd, "gitBranch": branch, "timestamp": ts,
                           "message": {"content": content}}, ensure_ascii=False)

    def title(self, text):
        return json.dumps({"type": "ai-title", "aiTitle": text})

    def write(self, rel, *lines, end="\n"):
        p = self.projects / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + end)
        return p

    def scan(self, scope=None, live=()):
        with mock.patch.object(hogs, "live_claude_sessions", return_value=([{"sessionId": s} for s in live], "")):
            return hogs.claude_conversations(scope or self.scope, self.projects, self.cache)

    def test_title_last_start_branches_and_subagents(self):
        self.write("-dev/s1.jsonl", self.rec("user", str(self.wt), "feat/checkout"), self.title("Primeiro"),
                   self.rec("assistant", str(self.scope / "shop-api"), "main", "2026-10-02T09:00:00.000Z", []),
                   self.title("Checkout em duas etapas"))
        self.write("-dev/s1/subagents/a.jsonl", self.rec("assistant", str(self.scope / "api"), "x",
                                                          "2026-10-03T08:00:00.000Z", []))
        convs, err = self.scan(live=["s1"])
        self.assertEqual(err, "")
        [c] = convs
        self.assertEqual((c.session, c.title, c.start, c.live), ("s1", "Checkout em duas etapas", str(self.wt), True))
        self.assertEqual(c.last, datetime(2026, 10, 3, 8, tzinfo=timezone.utc).timestamp())
        self.assertIn((str(self.wt), "feat/checkout"), c.branches)
        self.assertIn(str(self.scope / "api"), c.cwds)

    def test_mentions_only_from_work_under_scope_keep_spaces_and_accents(self):
        cmd = [{"type": "tool_use", "input": {"command": f"git -C '{self.wt}' status && ls {self.scope}/api/src"}}]
        out = [{"type": "tool_result", "content": f"{self.scope}/saida-de-comando"}]
        self.write("-dev/s1.jsonl", self.rec("assistant", content=cmd), self.rec("user", content=out),
                   self.rec("user", content=f"veja {self.scope}/shop-api/ção/nota.txt."))
        [c] = self.scan()[0]
        self.assertIn("shop-api/feature with spaces", c.mentions)
        self.assertIn("api/src", c.mentions)
        self.assertIn("shop-api/ção/nota.txt", c.mentions)
        self.assertFalse([m for m in c.mentions if m.startswith("saida")])

    def test_reads_only_appended_complete_lines(self):
        p = self.write("-dev/s1.jsonl", self.title("Antigo"))
        self.scan()
        first = p.read_bytes()
        p.write_bytes(b"x" * (len(first) - 32) + first[-32:])   # mesmo inode, tamanho e fim: não deve ser relido
        late = self.rec("user", ts="2026-10-05T10:00:00.000Z")
        with p.open("a") as f:
            f.write(late[:30])                                  # meia linha
        self.assertEqual(self.scan()[0][0].title, "Antigo")
        with p.open("a") as f:
            f.write(late[30:] + "\n")
        [c] = self.scan()[0]
        self.assertEqual(c.title, "Antigo")
        self.assertEqual(c.last, datetime(2026, 10, 5, 10, tzinfo=timezone.utc).timestamp())
        p.write_bytes(b"x" * (p.stat().st_size - 1) + b"\n")   # mesmo inode e tamanho, fim diferente: relido
        self.assertEqual(self.scan()[0][0].title, "")

    def test_corrupt_cache_other_scope_or_rewritten_file_rescans(self):
        p = self.write("-dev/s1.jsonl", self.title("A" * 50))
        self.scan()
        self.cache.write_text("{lixo")
        self.assertEqual(self.scan()[0][0].title, "A" * 50)
        data = json.loads(self.cache.read_text())
        data["files"][str(p)]["cwds"] = 5                          # JSON válido, entrada fora do formato
        self.cache.write_text(json.dumps(data))
        self.assertEqual(self.scan()[0][0].title, "A" * 50)
        p.write_text(self.title("B") + "\n")                       # menor, mesmo inode
        self.assertEqual(self.scan()[0][0].title, "B")
        p.write_text(self.rec("user", content=f"{self.wt}/a") + "\n" + self.title("C" * 80) + "\n")  # maior, mesmo inode
        self.assertEqual(self.scan()[0][0].title, "C" * 80)
        self.assertIn("feature with spaces/a", self.scan(self.scope / "shop-api")[0][0].mentions)

    def test_missing_dir_is_empty_and_unreadable_file_is_an_error(self):
        self.assertEqual(self.scan(), ([], ""))
        p = self.write("-dev/s1.jsonl", self.title("x"))
        p.chmod(0)
        self.addCleanup(p.chmod, 0o644)
        convs, err = self.scan()
        self.assertEqual(convs, [])
        self.assertIn("conversas do Claude Code", err)

    def test_every_mention_on_a_line_counts(self):
        s = self.scope
        self.assertEqual(hogs.mention_tails(f"ls {s}/a {s}/b".encode(), s), {"a", "b"})
        self.assertEqual(hogs.mention_tails(f"cp {s}/shop/a.txt {s}/api/b.txt".encode(), s),
                         {"shop/a.txt", "api/b.txt"})

    def fake_sessions(self, *files):
        home = self.projects.parent / "home"
        sessions = home / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        for name, text in files:
            (sessions / name).write_text(text)
        p = mock.patch.object(hogs, "HOME", home)
        p.start()
        self.addCleanup(p.stop)
        return sessions

    def test_unreadable_session_record_is_reported_and_conversations_are_kept(self):
        self.write("-dev/s1.jsonl", self.title("x"))
        self.fake_sessions(("a.json", "[1]"))
        self.assertIn("ilegível", hogs.live_claude_sessions()[1])
        convs, err = hogs.claude_conversations(self.scope, self.projects, self.cache)
        self.assertEqual([c.session for c in convs], ["s1"])
        self.assertIn("sessões abertas do Claude Code", err)
        with mock.patch.object(hogs, "live_claude_sessions", return_value=([], "registro ilegível: b.json")):
            convs, err = hogs.claude_conversations(self.scope, self.projects, self.cache)
        self.assertEqual(len(convs), 1)
        self.assertIn("b.json", err)

    def test_session_record_that_vanished_is_not_live(self):
        sessions = self.fake_sessions(("ok.json", '{"pid": %d, "sessionId": "s1"}' % os.getpid()))
        (sessions / "gone.json").symlink_to(sessions / "nao-existe.json")   # listado, some na leitura
        found, err = hogs.live_claude_sessions()
        self.assertEqual(([d["sessionId"] for d in found], err), (["s1"], ""))
        self.write("-dev/s1.jsonl", self.title("x"))
        [c], err = hogs.claude_conversations(self.scope, self.projects, self.cache)
        self.assertEqual((c.live, err), (True, ""))

    def test_resume_command_quotes_the_folder(self):
        c = hogs.Conversation("s1", start="/dev/feature with spaces")
        self.assertEqual(hogs.resume_command(c), "cd '/dev/feature with spaces' && claude --resume s1")
        self.assertEqual(hogs.resume_command(hogs.Conversation("s2")), "claude --resume s2")


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
