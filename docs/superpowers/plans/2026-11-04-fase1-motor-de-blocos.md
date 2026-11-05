# Fase 1: motor de blocos e regras seguras — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Abrir o "Sistema e outros" em blocos nomeados e recomendar, por bloco, só o que é comprovadamente seguro, no relatório estático e no `--clean` de hoje.

**Architecture:** Tudo continua em `hogs.py`. Cada bloco novo é uma função de coleta pura (testável com fixtures) + uma função que vira `Item`s para o fluxo de limpeza que já existe. A proteção por pastas montadas em containers é transversal: os blocos de arquivos consultam `under_mount()`. Fases 2 (worktrees + conversas) e 3 (tela interativa) têm planos próprios e consomem as interfaces daqui. A regra de ponto de volta pela etiqueta OCI de commit fica na fase 2, porque precisa achar o repositório local, que é o que a fase 2 faz; nesta fase vale a regra do prazo. "Apps fora da lista" continuam como informação na tabela "Outros grandes" que já existe.

**Tech Stack:** Python 3.9+, `rich` (única dependência), stdlib (`plistlib`, `fnmatch`, `os`, `subprocess`), ferramentas do macOS (`diskutil`, `sysctl`, `getconf`, `mdls`, `osascript`), Docker CLI.

**Spec:** `docs/superpowers/specs/2026-11-04-recomendacoes-seguras-e-tela-interativa-design.md`

## Global Constraints

- Um arquivo só: `hogs.py`. `install.sh` e `pyproject.toml` não mudam de forma.
- Só `rich` como dependência; Python 3.9 compatível (`from __future__ import annotations`, sem `match`, sem APIs 3.10+). CI roda 3.9 e 3.12.
- Texto de interface em pt-BR; tamanhos via `fmt()`; flags em inglês.
- Todo invariante novo de segurança: teste em `tests/test_hogs.py` + linha em `CLAUDE.md` (seção "Cleanup safety invariants").
- Toda remoção passa por seleção + `Confirm` (fluxo `clean()` existente); ações levantam erro em falha (`require`/`run_all`, ou exceção do Python).
- Arquivo pessoal nunca é apagado direto: só movido para a Lixeira.
- Ações recomendadas do macOS (reiniciar, atualizar) nunca são executadas e nunca viram `Item`.
- Na dúvida, risco maior.
- Testes, exemplos e screenshots só com nomes fictícios (repositório público).
- Versão 0.4.0 em `hogs.py` (`__version__`) e `pyproject.toml` juntas, na Task 8.
- Rodar a suíte inteira antes de cada commit: `python3 -m unittest discover -s tests` → `OK`.

## Review Focus

1. **Docker parado ou instalado sem rodar:** não dá para saber quais pastas os containers usam; o esperado é usar o último conjunto conhecido salvo e, sem ele, não sugerir nenhum arquivo pessoal nem cache (teste em Task 2 e Task 6).
2. **Arquivo do iCloud não baixado (dataless):** mostra tamanho mas não ocupa disco; o esperado é nunca aparecer como "dá para liberar" (tamanho = blocos alocados; teste em Task 6).
3. **Nome com espaço, aspas ou acento ao mover para a Lixeira:** o esperado é o caminho chegar intacto ao Finder (passado como `argv`, nunca interpolado no script; teste em Task 6).
4. **Tag de ponto de volta sem imagem atual da mesma origem, ou apontando para a mesma imagem da atual:** o esperado é não virar ponto de volta no primeiro caso (segue 🔴) e remover só a tag no segundo (`docker rmi repo:tag` exato; teste em Task 3).
5. **Arquivo `seen_tags.json` corrompido ou com data no futuro:** o esperado é tratar como visto agora (🟡), nunca como antigo (teste em Task 3).

---

### Task 1: Configuração com padrões e risco "para avaliar"

**Files:**
- Modify: `hogs.py` (perto de `Risk`, ~linha 82; perto de `load_config`, ~linha 1854)
- Test: `tests/test_hogs.py` (nova classe `SettingTest`)

**Interfaces:**
- Produces:
  - `EVAL = Risk("🟡", "para avaliar", "yellow", 1)`
  - `DEFAULTS: dict[str, object] = {"rollback_tags": ["antes", "backup", "bkp", "before", "rollback"], "rollback_days": 2, "stale_worktree_days": 30}`
  - `setting(key: str) -> object` — valor do `config.json` se tiver o mesmo tipo do padrão (lista de str; int > 0), senão o padrão.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s tests -k SettingTest`
Expected: FAIL/ERROR `has no attribute 'setting'`

- [ ] **Step 3: Implement `EVAL`, `DEFAULTS`, `setting(key: str) -> object` in `hogs.py`**

`bool` não conta como int válido.

- [ ] **Step 4: Run to verify it passes** — mesmo comando, `OK`.

- [ ] **Step 5: Commit** — `git commit -am "feat: configuração com padrões e risco para avaliar"`

---

### Task 2: Pastas montadas por containers protegem arquivos

**Files:**
- Modify: `hogs.py` (`Docker` ~linha 229, `gather_docker` ~linha 274; novas funções logo depois)
- Modify: `CLAUDE.md` (invariante)
- Test: `tests/test_hogs.py` (nova classe `MountsTest`; reusar `DockerCleanItemsTest.fake_docker`)

**Interfaces:**
- Consumes: `read_json`, `DATA_DIR`, `sh`.
- Produces:
  - `Docker.mounts: list[Path]` — origens de bind mount de **todos** os containers (rodando ou parados), resolvidas.
  - `Docker.mounts_ok: bool` — `True` só se o inspect respondeu.
  - `MOUNTS_FILE = DATA_DIR / "mounts.json"`
  - `known_mounts(d: Docker) -> tuple[list[Path], bool]` — se `d.mounts_ok`: salva e devolve `(d.mounts, True)`; senão devolve o último salvo `(salvos, True)`; sem arquivo salvo e com Docker instalado (`shutil.which("docker")`): `([], False)`; sem Docker instalado: `([], True)`.
  - `under_mount(path: Path, mounts: list[Path]) -> bool` — `path` dentro de um mount **ou** um mount dentro de `path`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m unittest discover -s tests -k MountsTest` → ERROR (atributos inexistentes).

- [ ] **Step 3: Implement**

Em `gather_docker`, depois de ler `containers`: um `docker inspect --format '{{json .Mounts}}' <todos os IDs>` (uma linha por container); só `Type == "bind"`. Falha de `sh` ou JSON inválido → `mounts_ok = False`. Sem containers → `mounts_ok = True`, lista vazia. Implementar `known_mounts` e `under_mount` como nas Interfaces (salvar como lista de strings).

- [ ] **Step 4: Run to verify it passes** — `-k MountsTest` → `OK`; suíte inteira → `OK`.

- [ ] **Step 5: CLAUDE.md** — acrescentar: "Any host folder bind-mounted by any container (running or stopped) — and anything inside or containing it — is never suggested by file-based blocks. Mounts unknown (Docker installed, not answering, nothing saved in `mounts.json`) → no file suggestions at all."

- [ ] **Step 6: Commit** — `git commit -am "feat: pastas montadas por containers ficam protegidas"`

---

### Task 3: Pontos de volta de imagens (tags `antes-*` e afins)

**Files:**
- Modify: `hogs.py` (`gather_docker` para `created_at`; `docker_clean_items` ~linha 410; novas funções antes dela)
- Modify: `CLAUDE.md` (exceção ao invariante de imagem local), `README.md` (garantias)
- Test: `tests/test_hogs.py` (nova classe `RollbackTest`)

**Interfaces:**
- Consumes: `setting("rollback_tags")`, `setting("rollback_days")`, `EVAL`, `run_all`, `image_ref`.
- Produces:
  - imagem ganha `"created_at": float | None` (epoch, de `CreatedAt` do `docker system df -v`, formato `"2026-11-04 20:04:12 -0300 -03"`).
  - `rollback_base(tag: str, words: list[str]) -> str | None` — `"<w>-..."` → `"latest"`; `"<base>-<w>-..."` → `"<base>"`; senão `None`. Comparação sem diferenciar maiúsculas.
  - `tag_date(tag: str) -> float | None` — `AAAAMMDD-HHMM` ou `AAAAMMDD` no nome, hora local.
  - `SEEN_TAGS = DATA_DIR / "seen_tags.json"`; `first_seen(refs: list[str], now: float) -> dict[str, float]` — carrega, adiciona refs novas com `now`, descarta as que sumiram, salva; arquivo ilegível ou data > `now` → `now`.
  - `rollback_age_days(img: dict, current: dict, seen: float, now: float) -> float` — idade a partir do sinal **mais recente** entre `tag_date`, `current["created_at"]` e `seen`.
  - Itens: `Item("docker", f"Ponto de volta {img['name']}", size, SAFE|EVAL, action=run_all([["docker","rmi",image_ref(img)]]), hint)` — um por imagem; SAFE se idade > `rollback_days`, hint `"ponto de volta há {n} dias"`; senão EVAL, hint `"ponto de volta recente: o QA pode estar em andamento"`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m unittest discover -s tests -k RollbackTest` → ERROR.

- [ ] **Step 3: Implement** as funções das Interfaces; em `docker_clean_items`, separar pontos de volta (tag com `rollback_base` e imagem `repo:base` presente em `d.images`) antes do laço de imagens, chamando `first_seen` uma vez com todas as refs de ponto de volta, **e só se houver alguma** (os testes antigos de `docker_clean_items` não podem gravar no `DATA_DIR` real). Pontos de volta saem do laço de 🔴 e de `small_pullable`. `created_at` ilegível → `None`.

- [ ] **Step 4: Run to verify it passes** — `-k RollbackTest` e `-k DockerCleanItemsTest` → `OK`.

- [ ] **Step 5: CLAUDE.md e README** — CLAUDE.md, na linha de "Locally built images…": acrescentar "Exception: rollback tags (`rollback_base()` with the current `repo:base` image present) are one item each, 🟢 after `rollback_days` (age = youngest of tag date, current image `Created`, first seen), 🟡 before." README › Garantias: uma linha explicando pontos de volta (`antes-*`, `backup-*`…) e o prazo configurável.

- [ ] **Step 6: Commit** — `git commit -am "feat: pontos de volta de imagens viram lixo depois do prazo"`

---

### Task 4: Bloco macOS e swap (só recomendações)

**Files:**
- Modify: `hogs.py` (nova seção "sistema" antes de "coleta")
- Test: `tests/test_hogs.py` (nova classe `SystemTest`, fixtures inline)

**Interfaces:**
- Produces:
  - `@dataclass SystemInfo: volumes: dict[str, int]; swap_used: int; uptime_days: float; update_pending: bool`
  - `parse_apfs(data: bytes, container: str) -> dict[str, int]` — do `diskutil apfs list -plist`: `CapacityInUse` somado por primeiro papel (`Roles[0]`) dos volumes do container cujo `ContainerReference == container`.
  - `parse_swap(text: str) -> int` — de `vm.swapusage` (`"used = 13799,12M"`; aceitar vírgula ou ponto; `M` = MiB).
  - `parse_boottime(text: str, now: float) -> float` — dias desde `{ sec = N, ... }`.
  - `gather_system(now: float) -> SystemInfo | None` — `diskutil info -plist /` → `APFSContainerReference`; `diskutil apfs list -plist`; `sysctl -n vm.swapusage`; `sysctl -n kern.boottime`; `diskutil apfs listSnapshots -plist /` → `update_pending` se algum `SnapshotName == "com.apple.os.update-MSUPrepareUpdate"`. Qualquer falha de `diskutil` → `None`; falha de `sysctl` → 0.
  - `SWAP_HIGH = 4e9`, `UPTIME_LONG_DAYS = 14`, `PREBOOT_NORMAL = 6e9`
  - `system_advice(si: SystemInfo) -> list[tuple[str, int, str]]` — `(rótulo, estimativa em bytes, como fazer)`:
    - reiniciar se `swap_used >= SWAP_HIGH` e `uptime_days >= UPTIME_LONG_DAYS`: `("Reiniciar o Mac", swap_used, f"swap {fmt(swap_used)} · ligado há {int(uptime_days)} dias")`
    - atualizar se `update_pending`: `("Aplicar a atualização do macOS", max(0, Preboot − PREBOOT_NORMAL), "Ajustes do Sistema › Geral › Atualização de Software")`

- [ ] **Step 1: Write the failing tests**

```python
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
```

(adicionar `import plistlib` no topo dos testes.)

- [ ] **Step 2: Run to verify it fails** — `-k SystemTest` → ERROR.
- [ ] **Step 3: Implement** as funções das Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k SystemTest` → `OK`.
- [ ] **Step 5: CLAUDE.md** — "macOS advice (restart, apply update) is text only: never executed, never an `Item`."
- [ ] **Step 6: Commit** — `git commit -am "feat: bloco macOS e swap com recomendações"`

---

### Task 5: Cópias de assinatura de apps (`*.code_sign_clone`)

**Files:**
- Modify: `hogs.py` (nova seção depois de "apps menos usados")
- Test: `tests/test_hogs.py` (nova classe `SignCloneTest`, árvore em `tempfile`)

**Interfaces:**
- Consumes: `process_running`, `du`, `SAFE`, `Item`.
- Produces:
  - `APPLICATIONS = Path("/Applications")`
  - `sign_clone_root() -> Path | None` — `getconf DARWIN_USER_TEMP_DIR` → pai → `"X"`; `None` se não existir.
  - `@dataclass CloneGroup: app: str; executable: str; installed: str; by_version: dict[str, list[Path]]`
  - `gather_sign_clones(root: Path) -> list[CloneGroup]` — para cada `root/*.code_sign_clone/code_sign_clone.*`: o primeiro `*/Contents/Info.plist` até 2 níveis abaixo; `app` = nome da pasta do bundle sem `.bundle` e sem `.app`; versão = `CFBundleShortVersionString`; `installed` = versão de `APPLICATIONS/<app>.app/Contents/Info.plist` (ou `""`).
  - `sign_clone_items(groups: list[CloneGroup]) -> list[Item]` — um item por app com versões ≠ `installed`: `Item("apps", f"Cópias antigas do {app} ({n})", tamanho, SAFE, action, hint, guard)`; tamanho = soma de `du` de **uma** cópia por versão antiga (as da mesma versão são clones entre si); action = `shutil.rmtree` (sem `ignore_errors`) de todas as cópias das versões antigas; guard = `f"feche o {app} antes"` se `process_running(executable)`; hint = `f"versões {', '.join(sorted(antigas))}; a instalada é {installed or 'nenhuma'}"`. Cópias da versão instalada nunca viram item.

- [ ] **Step 1: Write the failing test**

```python
class SignCloneTest(unittest.TestCase):
    def make(self, root, clone, app, version, exe="App"):
        c = root / "com.example.App.code_sign_clone" / clone / f"{app}.app.bundle" / "Contents"
        c.mkdir(parents=True)
        (c / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version, "CFBundleExecutable": exe}))
        (c / "blob").write_bytes(b"x" * 4096)

    def test_only_old_versions_one_copy_counted_and_guarded(self):
        tmp = Path(tempfile.mkdtemp())
        apps, root = tmp / "Applications", tmp / "X"
        (apps / "Navegador.app/Contents").mkdir(parents=True)
        (apps / "Navegador.app/Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "156"}))
        for n, v in (("a", "156"), ("b", "156"), ("c", "155"), ("d", "155"), ("e", "152")):
            self.make(root, f"code_sign_clone.{n}", "Navegador", v)
        with mock.patch.object(hogs, "APPLICATIONS", apps), \
                mock.patch.object(hogs, "du", side_effect=lambda p: 1_000_000_000):
            items = hogs.sign_clone_items(hogs.gather_sign_clones(root))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item.label, item.size, item.risk), ("Cópias antigas do Navegador (3)", 2_000_000_000, hogs.SAFE))
        with mock.patch.object(hogs, "process_running", return_value=True):
            self.assertEqual(item.guard(), "feche o Navegador antes")
        item.action()
        left = sorted(p.name for p in (root / "com.example.App.code_sign_clone").iterdir())
        self.assertEqual(left, ["code_sign_clone.a", "code_sign_clone.b"])
```

- [ ] **Step 2: Run to verify it fails** — `-k SignCloneTest` → ERROR.
- [ ] **Step 3: Implement** as funções das Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k SignCloneTest` → `OK`.
- [ ] **Step 5: Commit** — `git commit -am "feat: cópias antigas de assinatura de apps (Chrome etc.)"`

---

### Task 6: Arquivos pessoais parados → Lixeira

**Files:**
- Modify: `hogs.py` (nova seção depois da Task 5)
- Modify: `CLAUDE.md`, `README.md`
- Test: `tests/test_hogs.py` (nova classe `PersonalTest`)

**Interfaces:**
- Consumes: `under_mount`, `known_mounts` (Task 2), `EVAL`, `require`, `sh`.
- Produces:
  - `PERSONAL_ROOTS = [HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Pictures"]`
  - `STALE_PERSONAL_DAYS = 180`, `BIG_FILE = 500e6`
  - `SKIP_PACKAGES = (".photoslibrary", ".musiclibrary", ".app", ".tvlibrary")`
  - `spotlight_last_used(path: Path) -> float | None` — `mdls -name kMDItemLastUsedDate -raw <path>`; `(null)`/falha → `None`.
  - `stale_personal(roots: list[Path], mounts: list[Path], now: float) -> list[tuple[Path, int, float]]` — `(arquivo, bytes alocados = st_blocks*512, último uso)`; percorre sem seguir links; pula pastas com sufixo de `SKIP_PACKAGES`, pastas com `.git` e qualquer `under_mount`; entra só arquivo com alocado ≥ `BIG_FILE` e último uso (`max(st_mtime, spotlight_last_used)`) anterior a `now - STALE_PERSONAL_DAYS*86400`. Pasta sem permissão: ignora.
  - `move_to_trash(path: Path) -> None` — `require(sh(["osascript", "-e", "on run argv", "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)', "-e", "end run", str(path)]), "Finder")`
  - `personal_items(found: list[tuple[Path, int, float]], now: float) -> list[Item]` — um por arquivo: `Item("apps", f"Parado: {short(p)}", size, EVAL, lambda: move_to_trash(p), f"sem abrir há {meses} meses · vai para a Lixeira")`.

- [ ] **Step 1: Write the failing tests**

```python
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
                return mock.Mock(st_blocks=files[p], st_mtime=st.st_mtime, st_mode=st.st_mode)
            return st
        with mock.patch.object(hogs.Path, "lstat", lstat), \
                mock.patch.object(hogs, "spotlight_last_used", return_value=None):
            found = hogs.stale_personal([self.root], [self.root / "db"], time.time())
        self.assertEqual([p.name for p, _, _ in found], ["video.mov"])
        self.assertEqual(found[0][1], 2_000_000 * 512)

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
```

O essencial do mock: `st_blocks` controlado por arquivo; o teste afirma que só `video.mov` entra e que o tamanho é `st_blocks*512`.

- [ ] **Step 2: Run to verify it fails** — `-k PersonalTest` → ERROR.
- [ ] **Step 3: Implement** as funções das Interfaces (usar `Path.lstat` para o teste conseguir controlar `st_blocks`).
- [ ] **Step 4: Run to verify it passes** — `-k PersonalTest` → `OK`.
- [ ] **Step 5: CLAUDE.md e README** — CLAUDE.md: "Personal files are only moved to the Trash (Finder, path via `argv`), never deleted; they are always 🟡 `EVAL`, never in `a`; size is allocated blocks (iCloud dataless files never count)." README › Garantias: "Arquivo pessoal só vai para a Lixeira, nunca é apagado direto."
- [ ] **Step 6: Commit** — `git commit -am "feat: arquivos pessoais parados vão para a Lixeira"`

---

### Task 7: Caches grandes de apps fora da lista

**Files:**
- Modify: `hogs.py` (`gather` ~linha 1392)
- Test: `tests/test_hogs.py` (nova classe `ToolCacheTest`)

**Interfaces:**
- Consumes: `scan_candidates` (já inclui filhos de `CACHES` não cobertos), `under_mount`, `REDOWN`.
- Produces:
  - `BIG_CACHE = 500e6`
  - `cache_items(sizes: dict[Path, int], mounts: list[Path]) -> list[Item]` — para cada `p` com `p.parent == CACHES`, tamanho ≥ `BIG_CACHE` e fora de mount: `Item("dev", f"Cache: {p.name}", size, REDOWN, lambda p=p: shutil.rmtree(p), "cache do app; ele recria quando precisar")`.
  - Esses caminhos saem de `Snapshot.others` (não aparecem duas vezes).

- [ ] **Step 1: Write the failing test**

```python
class ToolCacheTest(unittest.TestCase):
    def test_only_big_cache_children_outside_mounts(self):
        c = hogs.CACHES
        sizes = {c / "ferramenta-x": 2_800_000_000, c / "pequeno": 10_000_000, hogs.HOME / "outra": 9e9,
                 c / "montado": 3e9}
        items = hogs.cache_items(sizes, [c / "montado"])
        self.assertEqual([(i.label, i.risk) for i in items], [("Cache: ferramenta-x", hogs.REDOWN)])
```

- [ ] **Step 2: Run to verify it fails** — `-k ToolCacheTest` → ERROR.
- [ ] **Step 3: Implement** `cache_items`; em `gather`, montar os itens com os `sizes` já medidos e excluir esses caminhos de `others`.
- [ ] **Step 4: Run to verify it passes** — `-k ToolCacheTest` → `OK`.
- [ ] **Step 5: Commit** — `git commit -am "feat: caches grandes de apps fora da lista"`

---

### Task 8: Barra por partes, relatório com "Recomendado agora" e "Não mexo", docs e versão

**Files:**
- Modify: `hogs.py` (`Snapshot` ~linha 1383, `gather`, `header` ~linha 1648, `report` ~linha 1744, `clean_items` ~linha 1774, `snapshot_sizes`, `__version__`)
- Modify: `pyproject.toml` (versão), `README.md` (uso e garantias), `CLAUDE.md`, `docs/make_screenshots.py` + SVGs
- Test: `tests/test_hogs.py` (nova classe `BreakdownTest`; atualizar testes de `report` se quebrarem)

**Interfaces:**
- Consumes: tudo das Tasks 2–7.
- Produces:
  - `Snapshot` ganha: `system: SystemInfo | None = None`, `advice: list[tuple[str, int, str]] = []`, `mounts_known: bool = True`, `projects: int = 0`.
  - `gather` passa a: chamar `gather_system`, `known_mounts`, `gather_sign_clones`/`sign_clone_items`, `stale_personal`/`personal_items` (só se `mounts_known`), `cache_items`; medir `setting("worktrees_root")` (se configurado e existir) como `projects`.
  - `breakdown(s: Snapshot) -> list[tuple[str, int, str]]` — `(rótulo, bytes, cor)`, nesta ordem: `"Docker"`, `"Caches de dev"`, `"Apps e dados"`, `"Projetos"`, `"macOS e swap"` (soma dos volumes que não são `Data`), `"Outras pastas"` (soma de `others`), `"Não identificado"`, `"Livre"`. Partes sem dado valem 0. Se a soma das partes conhecidas passar do usado, reduzir proporcionalmente as partes do volume Data (Docker, caches, apps, projetos, outras) até caber; "Não identificado" = resto, nunca negativo. Garantia: soma de tudo menos "Livre" == usado.
  - `header` desenha a barra e a legenda a partir de `breakdown`, e acrescenta `"com o recomendado: {fmt(free + soma dos 🟢 de clean_items)} livres"`.
  - `report` ganha o painel `"✨ Recomendado agora"` (tabela: 🟢 de `clean_items` agrupados por categoria + linhas de `advice` com "↻" + uma linha `"Arquivos parados para avaliar (n)"` com o total dos `EVAL`) e o painel `"🛡 Não mexo"` (containers vivos, pastas montadas protegidas, worktrees ainda não mostrados nesta fase). Se `not mounts_known`: linha `"Docker não respondeu: não sei quais pastas os containers usam, então não sugiro arquivos."`
  - `clean_items` inclui os itens novos (SAFE, REDOWN, EVAL). `advice` nunca entra.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails** — `-k BreakdownTest` → ERROR.
- [ ] **Step 3: Implement** `Snapshot` (campos novos com padrão, para os testes antigos seguirem), `breakdown`, `header`, `report`, `gather`, `clean_items`.
- [ ] **Step 4: Run to verify it passes** — `-k BreakdownTest` → `OK`; suíte inteira → `OK`.
- [ ] **Step 5: Smoke test real (só leitura)** — `python3 hogs.py > /tmp/hogs-fase1.txt; echo $?` → `0`; conferir no arquivo que aparecem "macOS e swap", "✨ Recomendado agora" e "🛡 Não mexo". Não rodar `--clean` com confirmação.
- [ ] **Step 6: Docs, screenshots e versão** — `docs/make_screenshots.py`: acrescentar ao snapshot fictício um `SystemInfo`, `advice` com "Reiniciar o Mac", um ponto de volta `shop-api:antes-20260101-0900` e um item `Cópias antigas do Navegador (3)`; rodar `python3 docs/make_screenshots.py`. README: usage table e Garantias com os blocos novos (pontos de volta, Lixeira, pastas montadas, recomendações do macOS nunca executadas, `config.json`: `rollback_tags`, `rollback_days`, `stale_worktree_days`, `worktrees_root`). `__version__ = "0.4.0"` e `version = "0.4.0"`.
- [ ] **Step 7: Commit** — `git add -A hogs.py tests/test_hogs.py README.md CLAUDE.md pyproject.toml docs/ && git commit -m "v0.4.0: Sistema e outros por partes e recomendações seguras por bloco"`
