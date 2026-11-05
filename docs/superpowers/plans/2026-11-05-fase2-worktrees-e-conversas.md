# Fase 2: worktrees parados, conversas do Claude Code e etiqueta OCI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tornar os worktrees parados sem merge (mas enviados) recuperáveis sem risco, ligar cada worktree às conversas do Claude Code que trabalharam nele, levar os worktrees para o relatório principal e liberar pontos de volta do Docker pelo commit da etiqueta OCI.

**Architecture:** Tudo em `hogs.py`. Uma varredura única das transcrições do Claude Code (`claude_conversations`), com cache incremental por arquivo em `DATA_DIR`, produz `Conversation`s; `conversation_links` liga conversas a worktrees por pasta, branch e citação. `inspect_worktree` ganha a regra do "parado e enviado" e guarda cada verificação em `checks` (dado para a aba Worktrees da fase 3). O relatório principal recebe as linhas dos worktrees pelo `gather` (em paralelo com o `du`, só com `worktrees_root` configurado) e os 🟢 viram um único `Item` agrupado. A regra OCI reaproveita a descoberta de repositórios (`find_repos`) e `default_branch`.

**Tech Stack:** Python 3.9+, `rich` (única dependência), stdlib (`json`, `os`, `re`, `shlex`, `subprocess`), Git CLI (só leitura, sem rede), Docker CLI.

**Spec:** `docs/superpowers/specs/2026-11-04-recomendacoes-seguras-e-tela-interativa-design.md` (seções "🌳 Worktrees", "🐳 Docker › Pontos de volta", "Barra e relatório", "Genérico por padrão").

## Global Constraints

- Um arquivo só: `hogs.py`. `install.sh` e `pyproject.toml` não mudam de forma. Só `rich`; Python 3.9 (`from __future__ import annotations`, sem `match`, sem APIs 3.10+). CI: macOS, 3.9 e 3.12.
- Texto de interface em pt-BR; tamanhos via `fmt()`; flags em inglês.
- Worktree: remoção só por `git worktree remove` **sem** `--force`; a branch fica; reconferir tudo imediatamente antes de remover (agora também envio e conversas); verificação desconhecida bloqueia.
- O hogs nunca faz `git fetch` nem acessa a rede; "enviado" = HEAD contido numa referência remota **local**.
- O hogs só **lê** `~/.claude` (transcrições, `sessions`, `settings.json`); nunca grava lá nem altera settings.
- Testes nunca gravam no `DATA_DIR` real nem leem o `~/.claude` ou o `config.json` reais (Task 1, Step 1 cria a rede de proteção em `setUpModule`; caminhos novos são parâmetros ou constantes de módulo).
- Testes, docs e screenshots só com nomes fictícios (`shop-api`, `acme`, `feat/checkout`…); nunca nomes reais de repositórios, branches ou imagens do autor.
- Todo invariante novo: teste em `tests/test_hogs.py` + linha em `CLAUDE.md` ("Cleanup safety invariants").
- Toda remoção passa por seleção + `Confirm`; ações levantam erro em falha.
- Versão 0.5.0 em `__version__` (`hogs.py`) e `pyproject.toml` juntas, na Task 8.
- Antes de cada commit: `python3 -m unittest discover -s tests` → `OK`.
- Fora do escopo: tela interativa, `--report`, mouse (fase 3).
- **Premissa (fase 1, Task 8, em paralelo):** existem `Snapshot.system`, `.advice`, `.mounts_known`, `.mounts`, `.projects` (bytes de `du(worktrees_root)`), `projects_root() -> Path | None`, `breakdown(s) -> list[tuple[str, int, str]]`, `recommended_panel(s, cleanable)`, `protected_panel(s)`, `plural(n, one, many)`, `preview(names)`, `CAT_TITLES` (com `"worktree": "🌳 Worktrees"`), e `hogs --worktrees PASTA` grava `worktrees_root` no `config.json`. Se algum nome diferir no código, usar o do código e manter o comportamento descrito aqui.

## Review Focus

1. **Transcrição sendo escrita durante a leitura (última linha pela metade):** a linha incompleta é ignorada e lida inteira na próxima vez, sem corromper o cache (teste em Task 1).
2. **Caminho de worktree com espaço ou acento citado numa conversa:** a citação liga a conversa ao worktree certo, e `…/wt-a` não liga a `…/wt-a-2` (testes em Task 1 e Task 3).
3. **Cache de conversas corrompido, de outra pasta (`scope`) ou arquivo reescrito no mesmo inode (menor ou maior):** releitura completa daquele arquivo, nunca exceção nem dado velho (teste em Task 1).
4. **Etiqueta OCI com commit que não existe no clone local (sem `fetch`), commit fora da main ou valor estranho (`--output=…`):** volta para a regra dos 2 dias, nunca 🟢 pela etiqueta (teste em Task 7).
5. **Claude Code não instalado (sem `~/.claude`):** nenhuma conversa, nenhum erro, nenhuma linha de `cleanupPeriodDays` no 🛡 Prevenção; os vereditos seguem só pelo Git (testes em Task 1 e Task 6).

## Decisões tomadas neste plano

- **Cache de conversas em `DATA_DIR`:** medido na máquina do autor, ~1.300 transcrições somam ~3 GB e uma leitura completa leva ~11 s. O cache por arquivo (inode + offset + bytes finais) relê só o que foi acrescentado. As menções ficam relativas à pasta dos worktrees (`scope`); trocar de pasta refaz o cache.
- **Comando de reabrir:** usa o primeiro `cwd` da transcrição (`start`). É por ele que o `claude --resume` acha a conversa, e é ele que decide o aviso "reabra antes de apagar".
- **"Mesmo repositório" da regra da branch:** o `cwd` está em alguma pasta registrada do repositório. Na dúvida, liga a mais, o que só gera 🟡 ou informação a mais.
- **Sessão viva do Claude Code citando um worktree sem merge:** vira 🟡, mesmo que a conversa não tenha sido ligada.
- **Falha ao ler as conversas:** vira ⚪ só para worktree sem merge; para os mergeados não muda nada (verificação desconhecida bloqueia, como nos invariantes).
- **Rótulo do ⚪ no totalizador:** "com trabalho só aqui" (texto da aba Worktrees no spec), não "com alterações", porque o ⚪ inclui commit não enviado, `.env` e verificação inconclusiva.
- **🟢 de worktrees no relatório principal:** um `Item` só, que também aparece no `hogs --clean`. Cada remoção reconfere tudo.
- **Etiqueta OCI:** vale por imagem atual (`repo:latest` para `antes-*`, `repo:<tag>` para `<tag>-antes-*`), não pelo repositório inteiro. A revisão só é aceita se for hexadecimal.
- **`cleanupPeriodDays` configurado com 30 dias ou menos:** também recebe a recomendação de 365.
- **Desempenho do `hogs`:** os worktrees só são coletados com `worktrees_root` configurado, em paralelo com o `du`. A pasta dos projetos e os worktrees são medidos cada um com seu `du`, o que gasta mais disco, mas evita contar o mesmo espaço duas vezes.

---

### Task 1: Conversas do Claude Code (varredura única com cache incremental)

**Files:**
- Modify: `hogs.py` (seção "worktrees", logo depois de `claude_work` ~linha 831; `import shlex` no topo)
- Test: `tests/test_hogs.py` (`setUpModule`; nova classe `ConversationTest`)

**Interfaces:**
- Consumes: `HOME`, `DATA_DIR`, `claude_work`, `live_claude_sessions`, `read_json`.
- Produces:
  - `CONVERSATIONS_CACHE = DATA_DIR / "conversations.json"`, `MENTION_PARTS = 8`
  - `@dataclass Conversation: session: str; title: str = ""; start: str = ""; last: float = 0.0; cwds: set[str]; branches: set[tuple[str, str]]; mentions: set[str]; live: bool = False` — `start` = primeiro `cwd` da transcrição principal (é de lá que `claude --resume` acha a conversa); `last` = maior `timestamp` de registros `user`/`assistant` (sem nenhum legível: `mtime` do arquivo); `branches` = pares `(cwd, gitBranch)` com branch não vazia e ≠ `"HEAD"`; `mentions` = caminhos citados **no trabalho** (`claude_work`), relativos a `scope`, cortados em `MENTION_PARTS` partes.
  - `@dataclass ConvLink: conv: Conversation; how: str; reopen: bool` — `how` ∈ `"dentro"`, `"branch"`, `"citou"`; `reopen` = `start` dentro do worktree.
  - `claude_work_record(record: dict) -> bytes` — o corpo atual de `claude_work` sobre um registro já lido; `claude_work(line)` passa a chamá-la (cada linha é lida com `json.loads` uma vez só).
  - `mention_tails(work: bytes, scope: Path) -> set[str]` — para cada ocorrência de `bytes(scope) + b"/"`: o texto seguinte até o primeiro de `"'` `` ` `` `\` `\n` `\t` `<>|;&()[]{}` (espaço **não** termina), decodificado em UTF-8 (`errors="replace"`), sem `" .,:"` no fim, cortado em `MENTION_PARTS` partes separadas por `/`; vazios descartados.
  - `parse_iso(ts: str) -> float | None` — `"2026-11-04T12:34:56.789Z"` → epoch (trocar `Z` por `+00:00` antes de `datetime.fromisoformat`); ilegível → `None`.
  - `read_transcript(path: Path, entry: dict | None, scope: Path) -> dict` — devolve a entrada de cache do arquivo: `{"ino", "offset", "tail", "session", "main", "start", "title", "last", "cwds", "branches", "mentions"}` (listas no JSON; `tail` = hex dos até 32 bytes antes de `offset`). Se `entry` tem o mesmo `ino`, `offset <= tamanho` e os bytes antes de `offset` ainda batem com `tail` (arquivo reescrito no mesmo inode não passa), lê só de `offset` em diante e junta (uniões; `title` = último `aiTitle` lido; `last` = máximo); senão lê do início. Só linhas completas (até o último `\n`); `offset` novo = posição depois dele. Linha com JSON inválido: ignorada. `session` = nome do arquivo sem `.jsonl`, ou o nome da pasta avó para `<sessionId>/subagents/*.jsonl` (`main = False`).
  - `claude_conversations(scope: Path, projects: Path | None = None, cache: Path | None = None) -> tuple[list[Conversation], str]` — `projects` padrão `HOME / ".claude" / "projects"` e `cache` padrão `CONVERSATIONS_CACHE`, resolvidos **na chamada**. Arquivos: `projects/*/*.jsonl` e `projects/*/*/subagents/*.jsonl`. Cache: `{"v": 1, "scope": str(scope), "files": {caminho: entrada}}`; `v` ou `scope` diferentes, ou JSON ilegível → cache vazio. Junta as entradas por `session` (sessão sem arquivo principal é descartada); `live = True` se o `sessionId` está em `live_claude_sessions()`. Grava o cache com só os arquivos que ainda existem (arquivo temporário + `os.replace`; `OSError` ignorado). Sem pasta `projects` → `([], "")`. Arquivo que sumiu entre a listagem e a leitura → ignorado; outro `OSError` → `([], "não foi possível ler as conversas do Claude Code: <erro>")`. Lista ordenada por `last` decrescente.
  - `resume_command(c: Conversation) -> str` — `f"cd {shlex.quote(c.start)} && claude --resume {c.session}"`; sem `start`: `f"claude --resume {c.session}"`.

- [ ] **Step 1: Rede de proteção dos testes**

No topo de `tests/test_hogs.py`, depois do `import hogs`:

```python
def setUpModule():
    tmp = Path(tempfile.mkdtemp())
    for name, value in (("DATA_DIR", tmp), ("CONFIG", tmp / "config.json"), ("SEEN_TAGS", tmp / "seen_tags.json"),
                        ("MOUNTS_FILE", tmp / "mounts.json"), ("CONVERSATIONS_CACHE", tmp / "conversations.json")):
        if hasattr(hogs, name):
            p = mock.patch.object(hogs, name, value)
            p.start()
            unittest.addModuleCleanup(p.stop)
```

Run: `python3 -m unittest discover -s tests` → `OK` (nenhum teste antigo depende dos arquivos reais).

- [ ] **Step 2: Write the failing tests**

```python
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
        p.write_bytes(b"x" * (len(first) - 1) + b"\n")          # mesmo inode e tamanho: não deve ser relido
        late = self.rec("user", ts="2026-10-05T10:00:00.000Z")
        with p.open("a") as f:
            f.write(late[:30])                                  # meia linha
        self.assertEqual(self.scan()[0][0].title, "Antigo")
        with p.open("a") as f:
            f.write(late[30:] + "\n")
        [c] = self.scan()[0]
        self.assertEqual(c.title, "Antigo")
        self.assertEqual(c.last, datetime(2026, 10, 5, 10, tzinfo=timezone.utc).timestamp())

    def test_corrupt_cache_other_scope_or_rewritten_file_rescans(self):
        p = self.write("-dev/s1.jsonl", self.title("A" * 50))
        self.scan()
        self.cache.write_text("{lixo")
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

    def test_resume_command_quotes_the_folder(self):
        c = hogs.Conversation("s1", start="/dev/feature with spaces")
        self.assertEqual(hogs.resume_command(c), "cd '/dev/feature with spaces' && claude --resume s1")
        self.assertEqual(hogs.resume_command(hogs.Conversation("s2")), "claude --resume s2")
```

- [ ] **Step 3: Run to verify it fails** — `python3 -m unittest discover -s tests -k ConversationTest` → ERROR (`has no attribute 'claude_conversations'`).
- [ ] **Step 4: Implement** as funções e classes das Interfaces.
- [ ] **Step 5: Run to verify it passes** — `-k ConversationTest` → `OK`; suíte inteira → `OK` (os testes de `agent_activity` continuam passando com `claude_work` refatorada).
- [ ] **Step 6: Commit** — `git commit -am "feat: conversas do Claude Code com cache incremental"`

---

### Task 2: Veredito "parado e enviado" e verificações por worktree

**Files:**
- Modify: `hogs.py` (`Worktree` ~linha 608, nova `pushed_ref` depois de `merged_how`, `inspect_worktree` ~linha 956)
- Modify: `CLAUDE.md` (invariante)
- Test: `tests/test_hogs.py` (`WorktreeTest`: `age()` com parâmetro, novos testes)

**Interfaces:**
- Consumes: `ConvLink`, `Conversation` (Task 1); `git_at`, `default_branch`, `merged_how`, `elapsed`.
- Produces:
  - `Worktree` ganha `pushed: str = ""` (ref remota local que contém o HEAD, sem `refs/remotes/`; vazio = não comprovado), `conversations: list[ConvLink] = []`, `checks: list[tuple[bool, str]] = []`.
  - `pushed_ref(path: Path, head: str, branch: str) -> str | None` — `git for-each-ref --contains <head> --format=%(refname) refs/remotes`; falha → `None`; ignora refs terminadas em `/HEAD`; prefere `refs/remotes/<remoto>/<branch curta>`, senão a primeira em ordem; nenhuma → `""`.
  - `inspect_worktree(w, days, usage, agents=None, links: list[ConvLink] | None = None, conv_error: str = "", stale_days: int = 30) -> Worktree` — regras novas, nesta ordem dentro do fluxo atual:
    1. Sem merge e com `default_branch` conhecida: `pushed = pushed_ref(...)`; `None` → blocker `"não foi possível verificar se os commits foram enviados"`; `""` → blocker `f"merge não comprovado em {ref curta} e há commits não enviados"`; senão `result.pushed = pushed`.
    2. Depois de `last_activity`, só se `not merged and pushed`: mexido depois de `now - stale_days*86400` → wait `f"sem merge, mexido {elapsed(last_activity)} (em andamento; prazo {stale_days} d)"`; conversa ligada com `conv.last` dentro do prazo → wait `f"conversa do Claude Code {elapsed(conv.last)}: {conv.title or conv.session[:8]}"` (a mais recente); `result.sessions` não vazio → wait `f"conversa aberta do Claude Code: {', '.join(result.sessions)}"`; `conv_error` → blocker `conv_error`.
    3. `eligible = not blockers and not waits and bool(merged or pushed)`. Reason do 🟢 sem merge: `f"enviado a {pushed} · mexido {elapsed(last_activity)} · a branch fica"`.
    4. `result.conversations = links or []`.
    5. `checks`, um por verificação feita, nesta ordem, `(True, texto de sucesso)` ou `(False, texto do blocker/wait)`: `"tudo commitado"`; `f"já na main ({merged})"` ou `f"enviado a {pushed} (conferido nas referências locais de origin)"`; `"sem .env nem arquivo que o Git não recupera"`; `"nada rodando dentro, nenhum container"`; `f"sem mexer desde {dd/mm} ({n} dias)"` (sucesso se mergeado ou parado além de `stale_days`). Saídas antecipadas (checkout principal, travado…) deixam `checks` vazio.
  - Mergeado continua 🟢 como hoje: conversa recente, mexido recente e `stale_days` não o afetam; `days` (`--worktree-days`) vale para os dois tipos de 🟢.

- [ ] **Step 1: Write the failing tests** (em `WorktreeTest`; `age(self, days=90)` passa a aceitar a idade)

```python
    def commit_and_push(self, push=True):
        (self.path / "file.txt").write_text("trabalho sem merge\n")
        self.git(self.path, "commit", "-am", "work")
        if push:
            self.git(self.path, "update-ref", "refs/remotes/origin/feature", "HEAD")
        self.age()

    def link(self, days_ago, inside=False):
        c = hogs.Conversation("s9", "Rascunho do checkout", str(self.path) if inside else "/x",
                              time.time() - days_ago * 86400)
        return [hogs.ConvLink(c, "dentro" if inside else "citou", inside)]

    def test_stale_unmerged_pushed_is_safe_and_removal_keeps_branch(self):
        self.commit_and_push()
        row = self.inspect(days=0)
        self.assertTrue(row.eligible, row.reason)
        self.assertEqual((row.merged, row.pushed), ("", "origin/feature"))
        self.assertIn("a branch fica", row.reason)
        self.assertTrue(all(ok for ok, _ in row.checks))
        self.assertIn("enviado a origin/feature (conferido nas referências locais de origin)", [t for _, t in row.checks])
        self.quiet()
        hogs.remove_worktree(row, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.git(self.repo, "rev-parse", "feature"), row.head)

    def test_unpushed_dirty_or_protected_is_kept_at_any_age(self):
        self.commit_and_push(push=False)
        row = self.inspect(days=0)
        self.assertEqual(row.verdict, "manter")
        self.assertIn("não enviados", row.reason)
        self.git(self.path, "update-ref", "refs/remotes/origin/feature", "HEAD")
        for name in ("file.txt", ".env"):
            (self.path / name).write_text("só aqui\n")
            self.age()
            self.assertEqual(self.inspect(days=0).verdict, "manter", name)
            self.git(self.path, "checkout", "--", "file.txt")
            (self.path / ".env").unlink(missing_ok=True)

    def test_unmerged_recent_conversation_or_running_is_in_progress(self):
        self.commit_and_push()
        os.utime(self.path / "file.txt", None)
        self.assertTrue(self.inspect(days=0).reason.startswith("sem merge, mexido"))
        self.age()
        row = hogs.inspect_worktree(self.w, 0, ([], "", []), hogs.Agents(), links=self.link(2))
        self.assertEqual(row.verdict, "depois")
        self.assertTrue(row.reason.startswith("conversa do Claude Code"))
        self.assertTrue(hogs.inspect_worktree(self.w, 0, ([], "", []), hogs.Agents(), links=self.link(40)).eligible)
        cited = hogs.Agents(mentions={self.path: {"Claude Code apps-1"}})
        self.assertEqual(self.inspect(agents=cited, days=0).verdict, "depois")
        self.assertEqual(self.inspect(([(self.path / "file.txt", "vim (pid 1)")], "", []), days=0).verdict, "depois")

    def test_stale_window_is_a_parameter_and_conversation_error_blocks_only_unmerged(self):
        self.commit_and_push()
        self.age(20)
        args = (self.w, 0, ([], "", []), hogs.Agents())
        self.assertEqual(hogs.inspect_worktree(*args, stale_days=30).verdict, "depois")
        self.assertTrue(hogs.inspect_worktree(*args, stale_days=10).eligible)
        self.assertEqual(hogs.inspect_worktree(*args, conv_error="falhou", stale_days=10).verdict, "manter")

    def test_merged_ignores_conversations_and_stale_window(self):
        row = hogs.inspect_worktree(self.w, 0, ([], "", []), hogs.Agents(), links=self.link(0, inside=True),
                                    conv_error="falhou")
        self.assertTrue(row.eligible, row.reason)
        self.assertEqual(len(row.conversations), 1)

    def test_pushed_ref_prefers_own_branch_and_ignores_head(self):
        self.commit_and_push()
        head = self.git(self.path, "rev-parse", "HEAD")
        self.git(self.repo, "update-ref", "refs/remotes/origin/aaa", head)
        self.assertEqual(hogs.pushed_ref(self.path, head, "refs/heads/feature"), "origin/feature")
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/feature")
        self.assertEqual(hogs.pushed_ref(self.path, head, "refs/heads/feature"), "origin/aaa")
        self.git(self.repo, "update-ref", "-d", "refs/remotes/origin/aaa")
        self.assertEqual(hogs.pushed_ref(self.path, head, "refs/heads/feature"), "")
```

- [ ] **Step 2: Run to verify it fails** — `python3 -m unittest discover -s tests -k WorktreeTest` → FAIL/ERROR nos testes novos.
- [ ] **Step 3: Implement** `Worktree` (campos novos), `pushed_ref`, `inspect_worktree` como nas Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k WorktreeTest` → `OK` (inclusive `test_unmerged_commit_is_preserved` e `test_squash_missing_part_of_branch_is_preserved`, que procuram "merge não comprovado"); suíte inteira → `OK`.
- [ ] **Step 5: CLAUDE.md** — depois da linha "Worktree deletion uses…": "An unmerged worktree is 🟢 only when untouched for `stale_worktree_days`, HEAD contained in a local remote-tracking ref (never fetched), clean, no protected ignored files, nothing running, no live Claude Code session citing it and no linked Claude Code conversation within the window. Unpushed commits keep it ⚪ at any age; conversations unreadable → ⚪. Merged worktrees ignore conversations."
- [ ] **Step 6: Commit** — `git commit -am "feat: worktree parado e enviado pode sair, a branch fica"`

---

### Task 3: Ligar conversas aos worktrees na coleta e na reconferência

**Files:**
- Modify: `hogs.py` (nova `find_repos` e `conversation_links` antes de `gather_worktrees`; `gather_worktrees` ~linha 1051; `remove_worktree` ~linha 1093)
- Modify: `CLAUDE.md` (lista de reconferência)
- Test: `tests/test_hogs.py` (`WorktreeTest.quiet()` também mocka `claude_conversations`; nova classe `ConversationLinkTest`)

**Interfaces:**
- Consumes: `claude_conversations`, `Conversation`, `ConvLink` (Task 1); `inspect_worktree(…, links, conv_error, stale_days)` (Task 2); `setting("stale_worktree_days")`; `projects_root()` (fase 1).
- Produces:
  - `find_repos(root: Path) -> tuple[list[Path], list[str]]` — a caminhada atual de `gather_worktrees` (pula `node_modules`, `.git`, `target`, `.venv`, `vendor`; para na raiz de cada repositório; deduplica por `--git-common-dir`), devolvendo o checkout principal de cada repositório (`registered_worktrees(repo)[0]`, ou a pasta achada se falhar), ordenado, e os avisos.
  - `conversation_links(rows: list[Worktree], convs: list[Conversation], scope: Path, repo_dirs: dict[Path, list[Path]]) -> dict[Path, list[ConvLink]]` — por `w.path`: `"dentro"` se algum `cwd` está dentro do worktree; senão `"branch"` se algum `(cwd, b)` tem `b == w.branch` sem `refs/heads/` e `cwd` dentro de alguma pasta de `repo_dirs[w.repo]` (todas as pastas registradas do repositório, principal incluída); senão `"citou"` se alguma menção casa com o caminho do worktree relativo a `scope`. Menções: um único regex `^(rel1|rel2|…)` (mais longos primeiro) seguido de `(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])`, aplicado com `match` a cada menção (o mesmo limite de `agent_activity`). `reopen = inside(Path(conv.start), w.path)` se houver `start`. Cada lista ordenada por `conv.last` decrescente.
  - `gather_worktrees(root, days)` (mesma assinatura e retorno): usa `find_repos`; cria cada `Worktree` já com `head` e `branch` do registro; monta `repo_dirs`; chama `claude_conversations(root)` **uma vez**; `stale_days = setting("stale_worktree_days")`; passa `links`, `conv_error` e `stale_days` para cada `inspect_worktree`.
  - `remove_worktree(w: Worktree, days: int, scope: Path | None = None) -> None` — reconfere com conversas frescas: `scope or projects_root() or w.path.parent`; `claude_conversations(scope)`; `conversation_links([w], …, {w.repo: pastas registradas de w.repo})`; `inspect_worktree(w, days, worktree_usage(), links=…, conv_error=…, stale_days=setting("stale_worktree_days"))`. O resto como hoje.
  - `clean_worktrees` passa `root` para `remove_worktree`.

- [ ] **Step 1: Write the failing tests**

```python
class ConversationLinkTest(unittest.TestCase):
    def test_links_by_folder_branch_and_mention(self):
        dev = Path("/dev")
        a = hogs.Worktree(dev / "shop-api/wt a", dev / "shop-api", branch="refs/heads/feat/checkout")
        a2 = hogs.Worktree(dev / "shop-api/wt a-2", dev / "shop-api", branch="refs/heads/feat/outra")
        dirs = {dev / "shop-api": [dev / "shop-api", a.path, a2.path]}
        C = hogs.Conversation
        convs = [C("dentro", start=str(a.path / "src"), last=5, cwds={str(a.path / "src")}),
                 C("branch", start="/dev/shop-api", last=4, branches={("/dev/shop-api", "feat/checkout")}),
                 C("outro-repo", last=3, branches={("/dev/web", "feat/checkout")}),
                 C("citou", last=2, mentions={"shop-api/wt a/x.txt"}),
                 C("citou-2", last=1, mentions={"shop-api/wt a-2/y", "shop-api/wt a.bak"})]
        links = hogs.conversation_links([a, a2], convs, dev, dirs)
        self.assertEqual([(l.conv.session, l.how, l.reopen) for l in links[a.path]],
                         [("dentro", "dentro", True), ("branch", "branch", False), ("citou", "citou", False)])
        self.assertEqual([l.conv.session for l in links[a2.path]], ["citou-2"])
```

E em `WorktreeTest`:

```python
    def test_find_repos_returns_main_checkouts_once(self):
        self.assertEqual(hogs.find_repos(self.root), ([self.repo], []))

    def test_gather_links_conversations_and_recheck_sees_new_ones(self):
        self.quiet()
        (self.path / "file.txt").write_text("trabalho\n")
        self.git(self.path, "commit", "-am", "work")
        self.git(self.path, "update-ref", "refs/remotes/origin/feature", "HEAD")
        self.age()
        old = hogs.Conversation("s1", "Antiga", str(self.path), time.time() - 60 * 86400, cwds={str(self.path)})
        with mock.patch.object(hogs, "claude_conversations", return_value=([old], "")) as cc:
            [row], _ = hogs.gather_worktrees(self.root, 0)
        cc.assert_called_once()
        self.assertTrue(row.eligible, row.reason)
        self.assertEqual([(l.how, l.reopen) for l in row.conversations], [("dentro", True)])
        new = hogs.Conversation("s2", "Nova", "/x", time.time(), cwds={str(self.path)})
        with mock.patch.object(hogs, "claude_conversations", return_value=([new], "")):
            with self.assertRaises(RuntimeError):
                hogs.remove_worktree(row, 0, self.root)
        self.assertTrue(self.path.exists())
```

`quiet()` passa a incluir `mock.patch.object(hogs, "claude_conversations", return_value=([], ""))` (os testes acima sobrepõem esse mock dentro do `with`).

- [ ] **Step 2: Run to verify it fails** — `python3 -m unittest discover -s tests -k ConversationLinkTest` e `-k WorktreeTest` → ERROR nos novos.
- [ ] **Step 3: Implement** `find_repos`, `conversation_links`, `gather_worktrees`, `remove_worktree`, `clean_worktrees` como nas Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k ConversationLinkTest`, `-k WorktreeTest` → `OK`; suíte inteira → `OK`.
- [ ] **Step 5: CLAUDE.md** — na linha "Recheck HEAD, branch, merge, activity…": acrescentar "pushed state and linked Claude Code conversations" à lista.
- [ ] **Step 6: Commit** — `git commit -am "feat: conversas ligadas a cada worktree, também na reconferência"`

---

### Task 4: Quadro `hogs --worktrees` com Conversa, envio e vereditos novos

**Files:**
- Modify: `hogs.py` (`worktrees_report` ~linha 1122, `CATEGORIES`/`worktree_category` ~linha 1153, `worktrees_summary` ~linha 1190, `clean_worktrees` ~linha 1249, `run`)
- Test: `tests/test_hogs.py` (nova classe `WorktreeReportTest`, só com `Worktree`s montados à mão)

**Interfaces:**
- Consumes: `Worktree.pushed`, `.conversations` (Tasks 2–3), `resume_command`, `elapsed`, `where`.
- Produces:
  - `conversation_cell(w: Worktree) -> Text` — `"—"` sem conversa; senão a mais recente: `"● "` se `live`, título ou `"(sem título)"`, e em `dim` na linha de baixo `elapsed(last)`, `f" · +{n-1}"` se houver mais e `" · ⚠ reabra antes de apagar"` se alguma tem `reopen`.
  - `worktrees_report(con, rows, root=None, days=0, stale_days=30)` — coluna nova **Conversa** (ratio 2, `overflow="fold"`) depois de "Em uso por"; "Na main?" mostra `✓ {merged}`, ou `✗ sem merge` + linha `✓ enviado ({pushed})` em amarelo, ou `✗ não comprovado`; 🟢 sem merge mostra `f"🟢 seguro · #{n}"` + linha `"a pasta sai, a branch fica"`.
  - `worktrees_summary(con, rows, root, days, stale_days=30)`:
    - 🟢 continua com o título `"🟢 SEGURO PARA APAGAR"`; o parágrafo atual vale para os mergeados; para os sem merge: `f"{n_worktrees(k)} sem merge, parados há mais de {stale_days} dias, com tudo commitado e enviado (conferido nas referências locais de origin): a pasta sai, a branch fica."`; e, se houver 🟢 com `reopen`, `"⚠ Reabra antes de apagar (a conversa começou dentro da pasta):"` + até 5 linhas `f"  {w.repo.name} / {where(w, root)}: {resume_command(conv)}"` e `f"  … mais {n}"`.
    - 🟡: título `"🟡 EM ANDAMENTO  "` e subtítulo `"  não mexa: em uso, mexido ou conversado há pouco"`; passo do "O que fazer": `"🟡 ficam: estão em uso ou em andamento (motivo na coluna Veredito). Rode de novo depois."`
    - Rodapé: `"Merge e envio conferidos nas referências locais de origin; rode git fetch nos repositórios para atualizá-las."`
  - `CATEGORIES` aceita `{d}` (formatado com `stale_days`): novas `"andamento": ("está sem merge e foi mexido nos últimos {d} dias", "estão sem merge e foram mexidos nos últimos {d} dias")`, `"conversa": ("está sem merge e tem conversa do Claude Code nos últimos {d} dias", "estão sem merge e têm conversa do Claude Code nos últimos {d} dias")`; `"merge"` passa a `("tem commits que não estão na main nem foram enviados", "têm commits que não estão na main nem foram enviados")`. `worktree_category`: wait começando com `"sem merge, mexido"` → `"andamento"`, com `"conversa"` → `"conversa"`; o resto como hoje.
  - `clean_worktrees`: na lista "Vou remover…", para cada link com `reopen`, a linha `f"    ⚠ reabra antes de apagar “{conv.title or conv.session[:8]}”: {resume_command(conv)}"` (antes do `Confirm`).
  - `run()` passa `setting("stale_worktree_days")` para `worktrees_report`.

- [ ] **Step 1: Write the failing tests**

```python
class WorktreeReportTest(unittest.TestCase):
    DEV = Path("/dev")

    def rows(self):
        conv = hogs.Conversation("s-123", "Relatório mensal", str(self.DEV / "shop-api-relatorio"),
                                 time.time() - 41 * 86400, live=True)
        parado = hogs.Worktree(self.DEV / "shop-api-relatorio", self.DEV / "shop-api", branch="refs/heads/feat/relatorio",
                               size=int(1.2e9), last_activity=time.time() - 41 * 86400, eligible=True,
                               pushed="origin/feat/rel", conversations=[hogs.ConvLink(conv, "dentro", True)])
        andamento = hogs.Worktree(self.DEV / "shop-api-cupom", self.DEV / "shop-api", branch="refs/heads/feat/cupom",
                                  waits=["sem merge, mexido há 3 d (em andamento; prazo 30 d)"])
        return [parado, andamento]

    def text(self, fn, *args):
        out = io.StringIO()
        fn(hogs.Console(file=out, width=300, force_terminal=False), *args)   # largo: nenhuma célula quebra
        return out.getvalue()

    def test_table_shows_conversation_pushed_and_new_verdicts(self):
        text = self.text(hogs.worktrees_report, self.rows(), self.DEV, 0, 30)
        for expected in ("Conversa", "● Relatório mensal", "reabra antes de apagar", "✓ enviado (origin/feat/rel)",
                         "seguro · #1", "a pasta sai, a branch fica", "EM ANDAMENTO",
                         "está sem merge e foi mexido nos últimos 30 dias",
                         "cd /dev/shop-api-relatorio && claude --resume s-123", "Merge e envio conferidos"):
            self.assertIn(expected, text)

    def test_clean_warns_to_reopen_before_confirm(self):
        with mock.patch.object(hogs.Prompt, "ask", return_value="a"), \
                mock.patch.object(hogs.Confirm, "ask", return_value=False):
            text = self.text(hogs.clean_worktrees, self.rows(), 0, self.DEV)
        self.assertIn("⚠ reabra antes de apagar “Relatório mensal”: "
                      "cd /dev/shop-api-relatorio && claude --resume s-123", text)
        self.assertIn("Nada apagado", text)
```

- [ ] **Step 2: Run to verify it fails** — `-k WorktreeReportTest` → FAIL.
- [ ] **Step 3: Implement** como nas Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k WorktreeReportTest` e `-k WorktreeTest` → `OK` (`"seguro · #1"` e `"SEGURO PARA APAGAR"` continuam); suíte inteira → `OK`.
- [ ] **Step 5: Commit** — `git commit -am "feat: quadro de worktrees com conversa, envio e vereditos novos"`

---

### Task 5: Worktrees no relatório principal (barra, totalizador, Recomendado, Não mexo)

**Files:**
- Modify: `hogs.py` (`Snapshot`, `gather`, `breakdown`, `snapshot_sizes`, `report`, `protected_panel`; novas funções perto de `safe_worktrees`)
- Test: `tests/test_hogs.py` (`BreakdownTest`; `ToolCacheTest._gather` passa a mockar `projects_root` → `None`)

**Interfaces:**
- Consumes: `gather_worktrees`, `safe_worktrees`, `remove_worktree(w, days, scope)` (Task 3); fase 1: `projects_root`, `Snapshot.projects`, `breakdown`, `recommended_panel`, `protected_panel`, `plural`.
- Produces:
  - `Snapshot` ganha `worktrees: list[Worktree] = []` (todas as linhas: dado da aba Worktrees da fase 3) e `worktrees_root: Path | None = None`.
  - `gather`: com `root = projects_root()`, submete `gather_worktrees(root, 0)` **primeiro** no mesmo `ThreadPoolExecutor` do `du`; status `"[bold]🌳 Conferindo worktrees…[/]"` enquanto espera; exceção → sem linhas (o relatório nunca cai por isso). Sem `root`: não chama. Acrescenta `worktree_item(rows, root)` a `items` quando não for `None`.
  - `verdict_counts(rows: list[Worktree]) -> dict[str, tuple[int, int]]` — `{"seguro"|"depois"|"manter": (quantos, bytes)}`, sempre com as três chaves.
  - `worktrees_line(rows: list[Worktree], root: Path | None) -> Text` — sem `root`: `"🌳 Worktrees: escolha a pasta dos seus projetos com hogs --worktrees PASTA (ela fica lembrada)"` em `dim`; sem linhas: `f"🌳 Worktrees: nenhum em {short(root)}"`; senão `f"🌳 Worktrees: {n} · {fmt(total)} · {a} podem sair ({fmt(sa)}) · {b} em andamento · {c} com trabalho só aqui"` + `f"  detalhes: hogs --worktrees {short(root)}"` em `dim`. `report` imprime logo depois do `header`.
  - `remove_worktrees(rows: list[Worktree], scope: Path) -> None` — `remove_worktree(w, 0, scope)` em cada um; depois de tentar todos, se algum falhou: `RuntimeError(f"{k} de {n} mantidos: {primeiro motivo}")`.
  - `worktree_item(rows: list[Worktree], root: Path) -> Item | None` — `None` sem 🟢; senão `Item("worktree", f"Worktrees que podem sair ({n})", soma, SAFE, lambda: remove_worktrees(safe, root), hint)`, `hint` = `"a branch fica"` + `f" · {a} na main"` / `f" · {b} enviados e parados"` (só as partes > 0) + `f" · reabra {r} conversas antes"` se houver `reopen`. Um item só (o `recommended_panel` mostra uma linha; o `--clean` também).
  - `breakdown(s)`, nesta ordem: `"Docker"`, `"Worktrees"` (`min(soma dos tamanhos de s.worktrees, s.projects)`, cor `"medium_purple1"`), `"Caches de dev"`, `"Apps e dados"`, `"Projetos"` (`s.projects - Worktrees`), `"macOS e swap"`, `"Outras pastas"`, `"Não identificado"`, `"Livre"`. Worktrees entra no grupo que encolhe proporcionalmente (volume Data). Soma menos "Livre" == usado, como antes.
  - `snapshot_sizes`: ignora `Item`s de `cat == "worktree"`; grava `"Worktrees"` e `"Projetos"` (já sem os worktrees).
  - `protected_panel`: com worktrees, linha `f"{plural(b, 'worktree em andamento', 'worktrees em andamento')} e {c} com trabalho só aqui: não mexo"` (só se `b` ou `c` > 0).

- [ ] **Step 1: Write the failing tests** (em `BreakdownTest`, reaproveitando `self.snap`)

```python
    def wt(self, size, verdict):
        w = hogs.Worktree(Path(f"/dev/wt-{size}-{verdict}"), Path("/dev/shop-api"), size=int(size))
        w.eligible = verdict == "seguro"
        w.waits = ["em uso agora: node"] if verdict == "depois" else []
        w.blockers = ["1 alterações locais ou arquivos não versionados"] if verdict == "manter" else []
        w.merged = "merge em origin/main" if w.eligible else ""
        return w

    def test_worktrees_split_from_projects_without_double_counting(self):
        for wts, expect in (((20e9, 5e9), (25e9, 35e9)), ((50e9, 30e9), (60e9, 0))):
            s = self.snap(92e9, 18e9, 60e9)
            s.projects, s.worktrees = int(60e9), [self.wt(x, "seguro") for x in wts]
            parts = dict((k, v) for k, v, _ in hogs.breakdown(s))
            self.assertEqual((parts["Worktrees"], parts["Projetos"]), expect)
            self.assertAlmostEqual(sum(v for k, v in parts.items() if k != "Livre"), 466e9, delta=1e6)
            self.assertGreaterEqual(parts["Não identificado"], 0)

    def test_worktrees_line_and_group_item(self):
        rows = [self.wt(1e9, "seguro"), self.wt(2e9, "seguro"), self.wt(4e9, "depois"), self.wt(8e9, "manter")]
        self.assertEqual(hogs.worktrees_line(rows, Path("/dev")).plain.split("  detalhes")[0],
                         "🌳 Worktrees: 4 · 15,0 GB · 2 podem sair (3,0 GB) · 1 em andamento · 1 com trabalho só aqui")
        self.assertIn("hogs --worktrees PASTA", hogs.worktrees_line([], None).plain)
        item = hogs.worktree_item(rows, Path("/dev"))
        self.assertEqual((item.label, item.size, item.risk), ("Worktrees que podem sair (2)", int(3e9), hogs.SAFE))
        self.assertIn("a branch fica", item.hint)
        calls = []
        def remove(w, days, scope):
            calls.append((w.size, days, scope))
            if w.size == int(1e9):
                raise RuntimeError("HEAD ou branch mudou desde a análise")
        with mock.patch.object(hogs, "remove_worktree", remove):
            with self.assertRaisesRegex(RuntimeError, "1 de 2 mantidos"):
                item.action()
        self.assertEqual(sorted(calls), [(int(1e9), 0, Path("/dev")), (int(2e9), 0, Path("/dev"))])
        self.assertIsNone(hogs.worktree_item([self.wt(4e9, "depois")], Path("/dev")))

    def test_group_counts_as_recommended(self):
        s = self.snap(92e9, 0, 0)
        s.items.append(hogs.worktree_item([self.wt(5e9, "seguro")], Path("/dev")))
        con = hogs.Console(file=io.StringIO(), width=140)
        con.print(hogs.header(s))
        self.assertIn("com o recomendado: 33,0 GB livres", con.file.getvalue())
```

E um teste de `gather` (mesmos mocks de `ToolCacheTest._gather`, mais `gather_system`/`clone_items`/`stale_personal` se a fase 1 não os mockar): com `projects_root` → `Path("/dev")` e `gather_worktrees` → `(rows, [])`, `snap.worktrees == rows`, `snap.worktrees_root == Path("/dev")` e `"Worktrees que podem sair (2)"` está nos rótulos de `snap.items`; com `projects_root` → `None`, `gather_worktrees.assert_not_called()` e `snap.worktrees == []`.

- [ ] **Step 2: Run to verify it fails** — `-k BreakdownTest` → ERROR.
- [ ] **Step 3: Implement** como nas Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k BreakdownTest`, `-k ToolCacheTest` → `OK`; suíte inteira → `OK`.
- [ ] **Step 5: CLAUDE.md** — "The main report's 🟢 worktrees are one `Item` (`worktree_item`); its action removes each one through `remove_worktree` (full recheck) and raises if any was kept. `breakdown()` counts worktrees once (inside Projetos' measurement, capped by it)."
- [ ] **Step 6: Commit** — `git commit -am "feat: worktrees no relatório principal, na barra e no recomendado"`

---

### Task 6: 🛡 Prevenção recomenda guardar as conversas do Claude Code

**Files:**
- Modify: `hogs.py` (`prevention_panel` ~linha 2107; nova `claude_retention` antes dela)
- Modify: `CLAUDE.md`
- Test: `tests/test_hogs.py` (nova classe `PreventionTest`)

**Interfaces:**
- Consumes: `HOME`, `read_json`, `load_config`.
- Produces:
  - `CLAUDE_KEEP_DAYS = 365`, `CLAUDE_DEFAULT_DAYS = 30`
  - `claude_retention() -> tuple[bool, str, str] | None` — `None` se `HOME / ".claude"` não é pasta; lê `HOME / ".claude" / "settings.json"` com `read_json`; `cleanupPeriodDays` int (não bool) `> CLAUDE_DEFAULT_DAYS` → `(True, f"Conversas do Claude Code guardadas por {n} dias", "")`; ausente, inválido ou `<= 30` → `(False, f"Conversas do Claude Code: apagadas depois de {n or 30} dias", f'para guardar mais, ponha "cleanupPeriodDays": {CLAUDE_KEEP_DAYS} em ~/.claude/settings.json (o hogs não altera)')`.
  - `prevention_panel(s)`: linhas do Docker só se `d.running or d.raw_real` (como hoje); linha de `claude_retention()` se não for `None`; sem nenhuma linha → `None`.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run to verify it fails** — `-k PreventionTest` → ERROR.
- [ ] **Step 3: Implement** `claude_retention` e o `prevention_panel` ajustado.
- [ ] **Step 4: Run to verify it passes** — `-k PreventionTest` → `OK`; suíte inteira → `OK`.
- [ ] **Step 5: CLAUDE.md** — "hogs only reads `~/.claude` (transcripts, sessions, settings); `cleanupPeriodDays` is a recommendation, never written."
- [ ] **Step 6: Commit** — `git commit -am "feat: prevenção recomenda guardar as conversas do Claude Code"`

---

### Task 7: Pontos de volta 🟢 pelo commit da etiqueta OCI

**Files:**
- Modify: `hogs.py` (`Docker` ~linha 231, `gather_docker` ~linha 305, `docker_clean_items` ~linha 557; novas funções antes de `docker_clean_items`; `gather`)
- Modify: `CLAUDE.md`, `README.md` (Garantias)
- Test: `tests/test_hogs.py` (nova classe `OciRollbackTest`)

**Interfaces:**
- Consumes: `find_repos` (Task 3), `default_branch`, `git_at`, `setting("rollback_tags")`, `projects_root()`.
- Produces:
  - `OCI_REVISION = "org.opencontainers.image.revision"`, `OCI_SOURCE = "org.opencontainers.image.source"`
  - imagem ganha `"labels": dict[str, str]` — em `gather_docker`, uma chamada `docker image inspect --format '{{.Id}}\t{{json .Config.Labels}}' <ids das imagens com tag>` (o comando começa com `["docker", "image", "inspect"]`, distinto do `["docker", "inspect"]` dos containers); mapeia pelo ID curto (12 caracteres sem `sha256:`); falha ou JSON inválido → `{}` para todas.
  - `Docker.released: set[str] = field(default_factory=set)` — nomes (`repo:tag`) das imagens atuais cujo commit já está na main de um clone local.
  - `rollback_pairs(d: Docker, words: list[str]) -> list[tuple[dict, dict]]` — o laço atual de `docker_clean_items` que acha `(ponto de volta, imagem atual)`, extraído sem mudar o comportamento.
  - `normalize_remote(url: str) -> str` — `"git@host:Org/Repo.git"`, `"https://host/org/repo(.git)(/)"`, `"ssh://git@host/org/repo.git"` → `"host/org/repo"` (minúsculas, sem usuário, porta e `.git`).
  - `released_images(d: Docker, root: Path | None) -> set[str]` — para cada imagem atual de `rollback_pairs` com as duas etiquetas e revisão `re.fullmatch(r"[0-9a-f]{7,64}", rev)`: procura em `find_repos(root)` (chamado **só** se houver alguma candidata e `root`) os repositórios com algum remoto (`git config --get-regexp ^remote\..*\.url$`) igual a `normalize_remote(source)`; `released` se `git merge-base --is-ancestor <rev> <default_branch>` devolve 0 em algum deles. Sem `root` → `set()`.
  - `gather` faz `docker.released = released_images(docker, root)` depois do `gather_docker`.
  - `docker_clean_items`: ponto de volta cuja imagem atual está em `d.released` → `SAFE`, hint `f"o commit da {current['name']} já está na main"`, qualquer idade (o caso "mesma imagem: só remove a tag" continua com tamanho 0 e o hint dele). Sem isso, a regra do prazo de hoje.

- [ ] **Step 1: Write the failing tests**

```python
class OciRollbackTest(unittest.TestCase):
    SRC = "https://github.com/acme/shop-api"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        repo = self.root / "shop-api"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
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

    def released(self, d, root="default"):
        with mock.patch.object(hogs, "load_config", return_value={}):
            return hogs.released_images(d, self.root if root == "default" else root)

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

    def test_without_labels_or_root_no_repo_scan(self):
        with mock.patch.object(hogs, "find_repos") as fr:
            self.assertEqual(self.released(self.docker({})), set())
            ok = {hogs.OCI_REVISION: self.on_main, hogs.OCI_SOURCE: self.SRC}
            self.assertEqual(self.released(self.docker(ok), root=None), set())
        fr.assert_not_called()

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
```

- [ ] **Step 2: Run to verify it fails** — `-k OciRollbackTest` → ERROR.
- [ ] **Step 3: Implement** como nas Interfaces.
- [ ] **Step 4: Run to verify it passes** — `-k OciRollbackTest`, `-k RollbackTest`, `-k DockerCleanItemsTest`, `-k MountsTest` → `OK`; suíte inteira → `OK`.
- [ ] **Step 5: CLAUDE.md e README** — CLAUDE.md, na linha da exceção dos pontos de volta: "Also 🟢 at any age when the current image's OCI `revision` (hex only) is an ancestor of the default branch of a local clone (under `worktrees_root`) whose remote matches the OCI `source`; checked locally, never fetched; anything else falls back to `rollback_days`." README › Garantias: uma linha com o mesmo sentido e o exemplo `docker build --label org.opencontainers.image.revision=$(git rev-parse HEAD) --label org.opencontainers.image.source=https://github.com/acme/shop-api .`
- [ ] **Step 6: Commit** — `git commit -am "feat: ponto de volta sai quando o commit da imagem atual está na main"`

---

### Task 8: Docs, screenshots e versão 0.5.0

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `docs/make_screenshots.py` + `docs/report.svg`, `docs/worktrees.svg`, `hogs.py` (`__version__`), `pyproject.toml`

**Interfaces:**
- Consumes: tudo das Tasks 1–7.

- [ ] **Step 1: Screenshots (só dados fictícios)** — em `docs/make_screenshots.py`: `wt()` ganha `pushed` e `conversations`; acrescentar em `WORKTREES` um `shop-api-relatorio` (`feat/relatorio`, 1,2 GB, 41 dias, `pushed="origin/feat/relatorio"`, `eligible=True`) com `Conversation("3f2a9c1e", "Relatório mensal por cliente", "/Users/ana/Development/shop-api-relatorio", …)` (caminho absoluto fictício: `resume_command` não expande `~`) e `reopen=True`, e um `shop-api-cupom` 🟡 `"sem merge, mexido há 3 d (em andamento; prazo 30 d)"` com conversa `live=True`; o `snapshot` ganha `worktrees=WORKTREES`, `worktrees_root=DEV` e o `Item` de `h.worktree_item(WORKTREES, DEV)`; mockar `h.claude_retention` para devolver a linha "não configurado". Rodar `python3 docs/make_screenshots.py` → `ok report.svg`, `ok apps.svg`, `ok worktrees.svg`. Conferir que nenhum nome real aparece (`grep` pelos SVGs só acha nomes do script).
- [ ] **Step 2: README** — seção "Worktrees Git": os três vereditos novos (🟢 já na main · 🟢 parado há +`stale_worktree_days` dias, sem merge mas enviado: a pasta sai, a branch fica · 🟡 em andamento · ⚪ com trabalho só aqui), "enviado" conferido nas referências locais (rode `git fetch`), coluna **Conversa** (título, data, ● aberta, ⚠ reabra antes de apagar com `cd … && claude --resume …`), como as conversas são ligadas (pasta, branch no mesmo repositório, caminho citado) e o cache em `~/Library/Application Support/hogs/conversations.json`. "O que o relatório mostra": o totalizador 🌳, a parte Worktrees da barra e o grupo no ✨ Recomendado; 🛡 Prevenção com `cleanupPeriodDays`. Tabela do `config.json`: `stale_worktree_days` deixa de ser "reservado"; `worktrees_root` também liga os worktrees no relatório principal e a regra OCI. Tabela de arquivos: `conversations.json`.
- [ ] **Step 3: Versão** — `__version__ = "0.5.0"` e `version = "0.5.0"`.
- [ ] **Step 4: Verificação** — `python3 -m unittest discover -s tests` → `OK`; `python3 hogs.py --version` → `hogs 0.5.0`; smoke real só leitura: `python3 hogs.py > "$TMPDIR/hogs-fase2.txt"; echo $?` → `0`, e o arquivo tem "🌳 Worktrees:" (ou a dica de escolher a pasta) e "🛡  Prevenção"; `python3 hogs.py --worktrees <pasta já lembrada> > "$TMPDIR/hogs-wt.txt"` → tem a coluna "Conversa". Rodar duas vezes e conferir que a segunda é bem mais rápida (cache de conversas). Nunca rodar `--clean` com confirmação.
- [ ] **Step 5: Commit** — `git add -A hogs.py tests/test_hogs.py README.md CLAUDE.md pyproject.toml docs/ && git commit -m "v0.5.0: worktrees parados e enviados, conversas do Claude Code e etiqueta OCI"`
