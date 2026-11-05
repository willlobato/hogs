# Fase 3: tela interativa (rich, empilhada, teclado + mouse) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `hogs` num terminal abre uma tela interativa com as abas Recomendado, Worktrees, Docker e Disco, em painéis empilhados, com teclado e mouse; fora de um terminal imprime o relatório estático. O fluxo numerado do `--clean` sai.

**Architecture:** Tudo em `hogs.py`, numa seção nova "tela". Camadas, cada uma testável sem terminal:
1. **terminal** (`Terminal`, `parse_input` → `Key`): modo cru, tela alternativa, mouse SGR, restauração garantida;
2. **lista** (`Row`, `Column`, `ListState`, `visible_rows`, marcação): árvore de linhas com grupos que abrem e "… mais N";
3. **desenho** (`render_lines`, `list_panel`, `stack` → `Frame`): cada painel vira linhas ANSI, e o `Frame` guarda qual linha da tela é qual linha da lista, para o clique;
4. **abas** (`recommended_tree`, `disk_tree`, `worktrees_tree`, `docker_tree`): dados → `(colunas, linhas)`;
5. **app** (`App`): estado, teclas, mouse, confirmação, execução por `apply_items`.

`App.frame(largura, altura)` e `App.handle(tecla)` são puros (sem terminal), o que permite testar toda a interação.

**Tech Stack:** Python 3.9+, `rich` (única dependência), stdlib (`termios`, `tty`, `select`, `contextlib`, `subprocess`).

**Spec:** `docs/superpowers/specs/2026-11-04-recomendacoes-seguras-e-tela-interativa-design.md` (seções "Tela interativa", "Abas", "Linha de comando", "Modelo de decisão", "Genérico por padrão", "Anti-metas").

## Global Constraints

- Um arquivo só: `hogs.py`. Só `rich` e stdlib (nada de Textual, curses opcional não). Python 3.9 (`from __future__ import annotations`, sem `match`, sem APIs 3.10+). CI: macOS, 3.9 e 3.12.
- Texto de interface em pt-BR; tamanhos via `fmt()`; flags em inglês.
- Nada vem marcado ao abrir. `a` marca só 🟢 (`SAFE`); 🟡 `EVAL`, 🟡 `REDOWN` e 🔴 `LOSS` só um por um. Ações do macOS (↻) e linhas de informação (⚪) nunca têm caixinha.
- Toda remoção: marcar → Enter → tela de confirmação (lista, total, aviso 🔴, aviso "reabra antes de apagar") → `s` → cada item reconferido na hora (`guard`, `remove_worktree`) → ✔/↷ por item. Ações levantam erro em falha (`require`/`run_all`).
- Todos os invariantes de `CLAUDE.md` continuam valendo (volumes, containers vivos, pastas montadas, worktrees sem `--force`, pessoais para a Lixeira, etc.). A tela só mostra e aciona `Item`s que já existem; não cria regra de risco nova.
- Fora de um terminal interativo (stdin ou stdout sem TTY, `TERM=dumb`, agente do launchd) ou com `--report`: relatório estático. A tela nunca abre sem TTY.
- O terminal sempre volta ao normal ao sair, inclusive por exceção ou Ctrl-C: tela alternativa desligada, cursor visível, mouse desligado, `termios` restaurado.
- Sem dica ao passar o mouse; sem layout lado a lado. `--no-mouse` desliga o mouse.
- O hogs nunca executa reinício nem atualização do macOS; Enter numa linha ↻ só mostra como fazer.
- Testes, docs e screenshots só com nomes fictícios (`shop-api`, `acme`, `feat/checkout`…). Testes nunca gravam no `DATA_DIR` real (a rede do `setUpModule` já cobre) e nunca abrem a tela num TTY real (usar `os.openpty`).
- Todo invariante novo: teste em `tests/test_hogs.py` + linha em `CLAUDE.md`.
- Versão 0.6.0 em `__version__` e `pyproject.toml` juntas (Task 9).
- Antes de cada commit: `python3 -m unittest discover -s tests` → `OK`.
- **Âncoras de merge:** o controlador cria, antes da onda 1, um commit-esqueleto com os imports novos e um cabeçalho de seção por tarefa em `hogs.py` e em `tests/test_hogs.py` (ver "Execução em paralelo"). Cada tarefa escreve **só logo abaixo do seu cabeçalho**, para as ondas paralelas não se tocarem.

## Review Focus

1. **Ctrl-C, exceção ou `q` no meio da tela:** o terminal volta exatamente ao estado anterior (sequências de saída escritas, `termios` igual ao de antes, exceção propagada). Testes na Task 1 (pty) e na Task 8 (`ctrl_c` sai do laço).
2. **Janela pequena ou redimensionada no meio:** abaixo de 60×20, ou se a lista ficaria com menos de 2 linhas, a tela mostra "Janela pequena demais…" em vez de layout quebrado; nada levanta exceção. Testes na Task 4 (`stack`) e na Task 8 (`App.frame(40, 10)`).
3. **Nada a mostrar** (Docker parado, sem `worktrees_root`, nenhum item, Claude Code não instalado): cada aba explica em uma linha, e ↑↓/espaço/Enter/a numa lista vazia não levantam exceção. Testes nas Tasks 5, 6, 7 e 8.
4. **Rótulos longos, acentos e emojis** (caminho de 200 caracteres, `🗂`, `ção`): cada linha da lista ocupa exatamente uma linha da tela, com todas as linhas da lista com a mesma largura visível, e o 🔎 mostra o texto inteiro, sem corte. Teste na Task 4.
5. **Algo muda entre marcar e confirmar** (app aberto, worktree mexido, comando falha): o item sai como ↷ com o motivo, nunca ✔, e continua na lista. Testes na Task 3 (`apply_items`) e na Task 8.
6. **Sequência de escape partida entre duas leituras** (`\x1b[<0;1` e depois `0;5M`): vira um clique só, sem teclas fantasmas. Teste na Task 1.

## Decisões tomadas neste plano

- **Abas com uma lista rolável cada.** O spec pede "um painel por projeto" (Worktrees) e "painéis separados" (Docker). Com altura fixa e um cursor só, cada aba tem **uma** lista rolável, em que projetos e blocos são **cabeçalhos de grupo** (▾/▸) com total; a borda e o título do painel ficam na lista. Custo se errado: visual menos "separado"; o dado é o mesmo.
- **Marca global por item.** A marca é guardada pela chave do item (`item_key`: `cat|label`) e vale em todas as abas. Um worktree marcado no Recomendado aparece marcado na aba Worktrees. O rodapé "Marcados" soma tudo.
- **Worktrees na tela: um `Item` por worktree** (`worktree_items`), filhos do grupo 🌳 no Recomendado e linhas da aba Worktrees. O relatório estático continua com o `Item` único (`worktree_item`), como hoje.
- **Guarda (`guard`) avaliada uma vez ao montar as abas** (ela roda `pgrep`). O texto fica na linha e no 🔎, e ela roda de novo em `apply_items`, na hora de apagar.
- **Depois de apagar:** itens ✔ somem das abas e da marca, e os ↷ continuam com o motivo no 🔎. Os números do topo não são remedidos: o rodapé diz "números da última medição; rode hogs de novo para medir".
- **Aba Disco** (a "segunda tela", deixada ao construtor): todos os itens por bloco (Docker, Caches de dev, Apps e dados, Worktrees, macOS e swap, Outros grandes), com qualquer risco, inclusive 🟡 que baixa de novo e 🔴. É onde fica tudo o que o `--clean` numerado oferecia. `e` leva até lá, no bloco do item em foco.
- **`hogs --worktrees PASTA`** num terminal abre a tela só com os worktrees de PASTA, sem medir o disco. As outras abas dizem "esta abertura é só dos worktrees de PASTA: rode hogs para o disco todo".
- **`--clean` sem terminal:** imprime o relatório estático e "a escolha do que apagar precisa de um terminal interativo"; sai com código 2.
- **Teclas:** Ctrl-C e `q` saem. Esc fecha ajuda e confirmação e, na lista, sai. Ctrl-Z é ignorado (ISIG desligado), para não deixar o terminal em modo cru.
- **Clique:** na barra de abas troca de aba. Numa linha da lista, leva o foco até ela; com `x < MARK_X` (a caixinha), também marca. A rodinha move o foco 3 linhas.
- **`r` reabrir:** suspende a tela (`Terminal.suspended()`) e roda `claude --resume <id>` com `cwd = conversa.start`. Se a pasta não existe mais, mostra "a pasta da conversa não existe mais; use c para copiar o comando". **`c`** copia por `pbcopy` (o comando de reabrir, se houver conversa; senão o caminho). **`o`** roda `open -R <caminho>`.
- **Saída de tela:** linhas posicionadas com `\x1b[{y};1H…\x1b[K`, num único `write`, envolvidas em atualização sincronizada (`\x1b[?2026h` … `\x1b[?2026l`; terminais sem suporte ignoram).

## Execução em paralelo

| Onda | Tarefas (em paralelo) | Depende de |
|---|---|---|
| 0 | esqueleto (controlador): imports + cabeçalhos de seção | — |
| 1 | Task 1 terminal · Task 2 lista · Task 3 itens, ações e detalhe | 0 |
| 2 | Task 4 desenho · Task 5 Recomendado e Disco · Task 6 Worktrees · Task 7 Docker | 1 (Tasks 5–7 usam Task 2 e Task 3; Task 4 usa Task 2) |
| 3 | Task 8 app | 1, 2 |
| 4 | Task 9 linha de comando, docs, screenshots, 0.6.0 | 3 |

Esqueleto (onda 0), em `hogs.py`: `import select`, `import termios`, `import tty` e `from contextlib import contextmanager` (em ordem alfabética com os existentes); `from typing import Callable, Iterator, NamedTuple, TextIO`; dentro do `try` do rich, `from rich.console import Console, Group, RenderableType` e `from rich.cells import cell_len`. Logo antes de `# ---------------------------------------------------------------- configuração e agente`, sete cabeçalhos separados por duas linhas em branco:
`# ---------------------------------------------------------------- tela: terminal` · `… tela: lista` · `… tela: itens e ações` · `… tela: desenho` · `… tela: abas Recomendado e Disco` · `… tela: aba Worktrees` · `… tela: aba Docker` · `… tela: app`.
Em `tests/test_hogs.py`, no fim: um comentário `# ---- Task N` para N = 1…9, separados por duas linhas em branco. Tasks que mudam código existente (3, 5, 7, 9) tocam regiões diferentes (ver cada uma).

---

### Task 1: Terminal e entrada (teclas e mouse)

**Files:**
- Modify: `hogs.py` (seção `tela: terminal`)
- Test: `tests/test_hogs.py` (`# ---- Task 1`: `ParseInputTest`, `TerminalTest`)

**Interfaces:**
- Produces:
  - `class Key(NamedTuple): name: str; x: int = -1; y: int = -1`. Nomes: `"up" "down" "left" "right" "enter" "space" "tab" "backtab" "esc" "backspace" "pgup" "pgdn" "home" "end" "ctrl_c" "click" "wheel_up" "wheel_down"`, ou o próprio caractere impresso (`"a"`, `"1"`, `"?"`, `"ç"`). Mouse: `x`, `y` 0-based.
  - `parse_input(data: bytes, final: bool = False) -> tuple[list[Key], bytes]`: devolve as teclas e o resto incompleto (sequência de escape ou UTF-8 pela metade). Com `final=True`, um `\x1b` sozinho vira `esc` e o resto incompleto é descartado.
  - Constantes `SCREEN_ON = "\x1b[?1049h\x1b[?25l"`, `SCREEN_OFF = "\x1b[?25h\x1b[?1049l"`, `MOUSE_ON = "\x1b[?1000h\x1b[?1006h"`, `MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"`.
  - `class Terminal`: `__init__(self, mouse: bool = True, fd: int | None = None, out: TextIO | None = None)` (padrões: `sys.stdin.fileno()` e `sys.stdout`); `__enter__` salva `termios.tcgetattr(fd)`, desliga `ICANON`, `ECHO`, `ISIG`, `IEXTEN` (lflag) e `IXON`, `ICRNL` (iflag), mantém `OPOST`, escreve `SCREEN_ON` (+ `MOUSE_ON` se `mouse`); `__exit__` escreve `MOUSE_OFF` (se `mouse`) + `SCREEN_OFF`, restaura o `termios` com `TCSAFLUSH`, nunca engole a exceção; `read(timeout: float) -> list[Key]` (`select` + `os.read(fd, 4096)`, acumula o resto; se sobrou só `b"\x1b"`, espera 0,03 s e chama `parse_input(…, final=True)`); `size() -> tuple[int, int]` (`shutil.get_terminal_size((80, 24))`); `draw(lines: list[str]) -> None` (um `write`: `\x1b[?2026h`, cada linha em `\x1b[{y+1};1H{linha}\x1b[K`, `\x1b[?2026l`; `flush`); `suspended() -> Iterator[None]` (`@contextmanager`: faz o que o `__exit__` faz, cede, e refaz o `__enter__`).

Mapa de escapes (CSI e SS3): `A B C D` → up/down/right/left; `H`, `1~`, `7~` → home; `F`, `4~`, `8~` → end; `5~` pgup; `6~` pgdn; `Z` backtab; `\x1bOA`… iguais aos CSI. `\r` e `\n` → enter; `" "` → space; `\t` → tab; `\x7f` e `\x08` → backspace; `\x03` → ctrl_c; outros controles < 0x20 ignorados. Sequência CSI desconhecida: consumida até o byte final (0x40–0x7E) e ignorada. Mouse SGR `\x1b[<b;x;y(M|m)`: `M` com `b == 0` → `click`; `b == 64` → `wheel_up`; `b == 65` → `wheel_down`; `m` (soltar), movimento (`b & 32`) e outros botões → ignorados.

- [ ] **Step 1: Write the failing tests**

```python
class ParseInputTest(unittest.TestCase):
    def names(self, data, final=False):
        keys, rest = hogs.parse_input(data, final)
        return [k.name for k in keys], rest

    def test_keys_and_text(self):
        self.assertEqual(self.names(b"\x1b[A\x1b[B\x1bOC\x1b[D \r\ta?\x7f\x03"),
                         (["up", "down", "right", "left", "space", "enter", "tab", "a", "?", "backspace", "ctrl_c"], b""))
        self.assertEqual(self.names(b"\x1b[5~\x1b[6~\x1b[H\x1b[F\x1b[Z"), (["pgup", "pgdn", "home", "end", "backtab"], b""))
        self.assertEqual(self.names("ç".encode()), (["ç"], b""))

    def test_mouse_sgr_click_and_wheel(self):
        keys, rest = hogs.parse_input(b"\x1b[<0;10;5M\x1b[<0;10;5m\x1b[<64;1;1M\x1b[<65;1;1M\x1b[<2;3;3M\x1b[<32;4;4M")
        self.assertEqual(keys, [hogs.Key("click", 9, 4), hogs.Key("wheel_up", 0, 0), hogs.Key("wheel_down", 0, 0)])
        self.assertEqual(rest, b"")

    def test_split_sequences_wait_for_the_rest(self):
        keys, rest = hogs.parse_input(b"a\x1b[<0;1")
        self.assertEqual(([k.name for k in keys], rest), (["a"], b"\x1b[<0;1"))
        keys, rest = hogs.parse_input(rest + b"0;5M")
        self.assertEqual((keys, rest), ([hogs.Key("click", 9, 4)], b""))
        self.assertEqual(self.names("ç".encode()[:1]), ([], "ç".encode()[:1]))

    def test_lone_escape_only_when_final(self):
        self.assertEqual(self.names(b"\x1b"), ([], b"\x1b"))
        self.assertEqual(self.names(b"\x1b", final=True), (["esc"], b""))

    def test_unknown_sequences_are_swallowed(self):
        self.assertEqual(self.names(b"\x1b[200~x\x1b[?1;2c"), (["x"], b""))


class TerminalTest(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = os.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.out = io.StringIO()

    def test_enter_and_exit_restore_everything(self):
        import termios
        before = termios.tcgetattr(self.slave)
        with hogs.Terminal(mouse=True, fd=self.slave, out=self.out):
            during = termios.tcgetattr(self.slave)
            self.assertFalse(during[3] & termios.ECHO)
            self.assertFalse(during[3] & termios.ISIG)
        self.assertEqual(termios.tcgetattr(self.slave), before)
        text = self.out.getvalue()
        self.assertTrue(text.startswith(hogs.SCREEN_ON + hogs.MOUSE_ON))
        self.assertTrue(text.endswith(hogs.MOUSE_OFF + hogs.SCREEN_OFF))

    def test_exception_inside_still_restores_and_propagates(self):
        import termios
        before = termios.tcgetattr(self.slave)
        with self.assertRaises(KeyboardInterrupt):
            with hogs.Terminal(mouse=False, fd=self.slave, out=self.out):
                raise KeyboardInterrupt
        self.assertEqual(termios.tcgetattr(self.slave), before)
        self.assertNotIn(hogs.MOUSE_ON, self.out.getvalue())
        self.assertTrue(self.out.getvalue().endswith(hogs.SCREEN_OFF))

    def test_read_draw_and_suspend(self):
        with hogs.Terminal(mouse=False, fd=self.slave, out=self.out) as term:
            os.write(self.master, b"\x1b[A")
            self.assertEqual(term.read(1.0), [hogs.Key("up")])
            os.write(self.master, b"\x1b")
            self.assertEqual(term.read(1.0), [hogs.Key("esc")])
            self.assertEqual(term.read(0.01), [])
            term.draw(["um", "dois"])
            with term.suspended():
                self.assertTrue(self.out.getvalue().endswith(hogs.SCREEN_OFF))
            self.assertTrue(self.out.getvalue().endswith(hogs.SCREEN_ON))
        self.assertIn("\x1b[1;1Hum\x1b[K\x1b[2;1Hdois\x1b[K", self.out.getvalue())
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.ParseInputTest tests.test_hogs.TerminalTest` → FAIL (`parse_input` não existe).
- [ ] **Step 3:** Implementar `Key`, `parse_input`, as constantes e `Terminal` como descrito em Interfaces.
- [ ] **Step 4:** Os mesmos testes → PASS; suíte inteira → `OK`.
- [ ] **Step 5:** Commit `feat(tela): terminal em modo cru com teclas, mouse SGR e restauração garantida`.

---

### Task 2: Lista em árvore (linhas, grupos, cursor e marcação)

**Files:**
- Modify: `hogs.py` (seção `tela: lista`)
- Test: `tests/test_hogs.py` (`# ---- Task 2`: `ListModelTest`)

**Interfaces:**
- Consumes: `Item`, `Risk`, `SAFE`, `Conversation`, `fmt`.
- Produces:
  - `class Column(NamedTuple): title: str; justify: str = "left"; ratio: int | None = None; width: int | None = None`.
  - `@dataclass class Row`: `key: str`; `cells: list` (`Text | str`, uma por coluna; a primeira é o rótulo); `item: Item | None = None`; `children: list[Row] = field(default_factory=list)`; `limit: int = 0` (>0: o grupo aberto mostra só os `limit` primeiros filhos e uma linha "… mais N"); `kind: str = "item"` (`"item" | "group" | "more" | "advice" | "info"`); `detail: list = field(default_factory=list)` (`Text`s do 🔎, sem corte); `title: str = ""` (nome no título do 🔎; vazio → texto da primeira célula); `path: Path | None = None`; `resume: Conversation | None = None`; `how: str = ""` (Enter numa linha ↻); `group: str = ""` (chave do grupo da aba Disco para `e`).
  - `item_key(i: Item) -> str` → `f"{i.cat}|{i.label}"`. Linhas de item usam `key = item_key(item)`.
  - `class Visible(NamedTuple): row: Row; depth: int; parent: Row | None`.
  - `@dataclass class ListState: cursor: int = 0; scroll: int = 0; expanded: set[str] = field(default_factory=set); flt: str = ""`.
  - `visible_rows(rows: list[Row], expanded: set[str]) -> list[Visible]`: pré-ordem; filhos só de grupos com `key in expanded`. Com `limit` e `f"{key}/+"` fora de `expanded`, mostra os `limit` primeiros e uma linha sintética `Row(key=f"{key}/+", cells=[Text(f"… mais {n}", style="dim"), fmt(soma dos tamanhos dos escondidos)], kind="more")`, com `depth` dos filhos e `parent` = o grupo.
  - `checkable(row: Row) -> bool`: `row.item is not None and row.item.action is not None and row.kind == "item"`.
  - `leaf_items(row: Row) -> list[Item]`: os itens marcáveis da subárvore, inclusive filhos escondidos pelo `limit` e grupos fechados.
  - `mark_box(row: Row, marked: set[str]) -> str`: `"[x]"` todos marcados, `"[-]"` alguns, `"[ ]"` nenhum, `""` nada marcável.
  - `toggle(row: Row, marked: set[str]) -> None`: se todos de `leaf_items(row)` estão marcados, desmarca todos; senão marca todos.
  - `mark_all_safe(rows: list[Row], marked: set[str]) -> int`: marca todo item marcável com `risk is SAFE` em toda a árvore (grupos fechados também); devolve quantos marcou agora.
  - `collect_items(rows: list[Row]) -> dict[str, Item]`: chave → item de todas as linhas marcáveis.
  - `move(state: ListState, total: int, delta: int, height: int) -> None`: cursor limitado a `[0, total-1]` (lista vazia: 0); `scroll` ajustado para o cursor ficar em `[scroll, scroll+height)`; `scroll` limitado a `[0, max(0, total-height)]`.
  - `open_row(state: ListState, vis: list[Visible]) -> None` (→): grupo fechado abre; grupo aberto leva o cursor ao primeiro filho; `more` adiciona `f"{parent.key}/+"` a `expanded`. `close_row(state: ListState, vis: list[Visible]) -> None` (←): grupo aberto fecha; linha com `parent` leva o cursor ao pai.

- [ ] **Step 1: Write the failing tests**

```python
class ListModelTest(unittest.TestCase):
    def item(self, label, size=1, risk=None, action=True):
        return hogs.Item("docker", label, size, risk or hogs.SAFE, (lambda: None) if action else None)

    def tree(self):
        kids = [hogs.Row(hogs.item_key(i), [i.label], i) for i in
                (self.item("a", 5), self.item("b", 4), self.item("c", 3, hogs.EVAL), self.item("d", 2, hogs.LOSS))]
        info = hogs.Row("info", ["só informação"], kind="info")
        return [hogs.Row("g", ["grupo"], kind="group", children=kids, limit=2), info]

    def test_visible_with_limit_and_more(self):
        rows = self.tree()
        self.assertEqual([v.row.key for v in hogs.visible_rows(rows, set())], ["g", "info"])
        vis = hogs.visible_rows(rows, {"g"})
        self.assertEqual([v.row.key for v in vis], ["g", "docker|a", "docker|b", "g/+", "info"])
        self.assertEqual((vis[3].row.kind, vis[3].depth, vis[3].parent.key), ("more", 1, "g"))
        self.assertIn("mais 2", str(vis[3].row.cells[0]))
        self.assertEqual(len(hogs.visible_rows(rows, {"g", "g/+"})), 6)

    def test_mark_all_safe_never_marks_eval_loss_or_info(self):
        rows, marked = self.tree(), set()
        self.assertEqual(hogs.mark_all_safe(rows, marked), 2)
        self.assertEqual(marked, {"docker|a", "docker|b"})
        self.assertEqual(hogs.mark_box(rows[0], marked), "[-]")
        self.assertEqual(hogs.mark_box(rows[1], marked), "")

    def test_toggle_group_and_leaf(self):
        rows, marked = self.tree(), set()
        hogs.toggle(rows[0], marked)
        self.assertEqual(len(marked), 4)
        self.assertEqual(hogs.mark_box(rows[0], marked), "[x]")
        hogs.toggle(rows[0], marked)
        self.assertEqual(marked, set())
        hogs.toggle(rows[1], marked)
        self.assertEqual(marked, set())

    def test_move_clamps_and_scrolls(self):
        st = hogs.ListState()
        hogs.move(st, 10, 7, 3)
        self.assertEqual((st.cursor, st.scroll), (7, 5))
        hogs.move(st, 10, 99, 3)
        self.assertEqual((st.cursor, st.scroll), (9, 7))
        hogs.move(st, 10, -99, 3)
        self.assertEqual((st.cursor, st.scroll), (0, 0))
        empty = hogs.ListState()
        hogs.move(empty, 0, 1, 3)
        self.assertEqual((empty.cursor, empty.scroll), (0, 0))

    def test_open_and_close(self):
        rows, st = self.tree(), hogs.ListState()
        hogs.open_row(st, hogs.visible_rows(rows, st.expanded))
        self.assertIn("g", st.expanded)
        hogs.open_row(st, hogs.visible_rows(rows, st.expanded))
        self.assertEqual(st.cursor, 1)
        hogs.close_row(st, hogs.visible_rows(rows, st.expanded))
        self.assertEqual(st.cursor, 0)
        st.cursor = 3
        hogs.open_row(st, hogs.visible_rows(rows, st.expanded))
        self.assertIn("g/+", st.expanded)
        st.cursor = 0
        hogs.close_row(st, hogs.visible_rows(rows, st.expanded))
        self.assertNotIn("g", st.expanded)

    def test_collect_items(self):
        self.assertEqual(sorted(hogs.collect_items(self.tree())), ["docker|a", "docker|b", "docker|c", "docker|d"])
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.ListModelTest` → FAIL.
- [ ] **Step 3:** Implementar o que está em Interfaces.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): lista em árvore com grupos, "… mais N", cursor e marcação`.

---

### Task 3: Itens da tela, execução e texto do 🔎

**Files:**
- Modify: `hogs.py`: `Item` (dois campos novos no fim); `cache_items`, `sign_clone_items`, `personal_items` e a lista de `PATH_DEFS` em `gather` (só passar `path=`); `clean()` passa a usar `apply_items` (o mesmo texto de saída); seção `tela: itens e ações`.
- Test: `tests/test_hogs.py` (`# ---- Task 3`: `TuiItemsTest`)

**Interfaces:**
- Consumes: `Worktree`, `ConvLink`, `remove_worktree`, `safe_worktrees`, `where`, `branch_name`, `elapsed`, `resume_command`, `VERDICTS`, `VERDICT_LABELS`, `clean_items`, `DATA_VOLUME`.
- Produces:
  - `Item.path: Path | None = None` e `Item.worktree: Worktree | None = None`, preenchidos assim: itens de `PATH_DEFS` → `pd.paths[0]`; `cache_items` → `p`; `personal_items` → `p`; `sign_clone_items` → a primeira cópia; `worktree_items` → `w.path` e `w`.
  - `worktree_items(rows: list[Worktree], root: Path, days: int = 0) -> list[Item]`: um por worktree de `safe_worktrees(rows)`, na mesma ordem. `Item("worktree", f"{w.repo.name} / {where(w, root)}", w.size, SAFE, lambda w=w: remove_worktree(w, days, root), hint, path=w.path, worktree=w)`. `hint`: `f"já na main ({w.merged})"` se `w.merged`, senão `f"enviado ({w.pushed}), parado {elapsed(w.last_activity)}"`; mais `" · ⚠ reabra antes de apagar"` se algum `link.reopen`.
  - `tui_items(s: Snapshot) -> list[Item]`: `clean_items(s)` sem os de `cat == "worktree"`, mais `worktree_items(s.worktrees, s.worktrees_root)` se houver `worktrees_root`, na ordem de `clean_items` (`risk.rank`, `-size`).
  - `apply_items(items: list[Item], notify: Callable[[Item, bool, str], None]) -> int`: para cada item, nesta ordem: se `guard` e `why := guard()`, chama `notify(i, False, f"pulei, {why}")`; senão roda `action()`; se der `OSError` ou `RuntimeError`, chama `notify(i, False, str(exc))`; senão, `notify(i, True, "")`. Devolve `max(0, livre depois − livre antes)` por `shutil.disk_usage(DATA_VOLUME)`. `clean()` passa a chamá-la, imprimindo as mesmas linhas ✔/↷ de hoje.
  - `RISK_WHY: dict[str, str]`, pela `Risk.label`: `"seguro": "volta sozinho ou já está guardado em outro lugar"`, `"baixa de novo": "volta baixando de novo"`, `"perde algo": "não volta: só apague se tiver certeza"`, `"para avaliar": "não volta sozinho: avalie você (arquivos vão para a Lixeira)"`, `"só informação": "só informação: o hogs não apaga"`.
  - `item_detail(i: Item, guard_text: str | None = None) -> list[Text]`: o rótulo inteiro (negrito); `f"{fmt(i.size)} · {i.risk.icon} {i.risk.label}: {RISK_WHY[i.risk.label]}"`; o `hint` inteiro, se houver; `✗ {guard_text}` (amarelo) se houver; `f"📁 {short(i.path)}"` com estilo `link file://{i.path}` se houver `path`; se houver `worktree`, mais as linhas de `worktree_detail(i.worktree, None)`.
  - `worktree_detail(w: Worktree, root: Path | None) -> list[Text]`, nesta ordem:
    - `f"{where(w, root)} · {branch_name(w)}"` e `f"{fmt(w.size)} · mexido {elapsed(w.last_activity)}"`;
    - cada `w.checks` como `✓ texto` (verde) ou `✗ texto` (amarelo);
    - `f"▶ em uso por: {', '.join(w.users)}"`, se houver; cada `w.waits` com `🟡 ` e cada `w.blockers` com `⚪ `;
    - o veredito (`VERDICTS` + `VERDICT_LABELS`) e `"a pasta sai, a branch fica"` quando `w.eligible`;
    - **todas** as conversas, da mais recente para a mais antiga: `f"💬 {'● ' if live else ''}“{título ou sessão[:8]}” · última msg {elapsed(last)}"`, o `resume_command` (estilo `cyan`) e, se `link.reopen`, `"⚠ reabra antes de apagar: depois da remoção ela não abre mais"` (amarelo).

- [ ] **Step 1: Write the failing tests**

```python
class TuiItemsTest(unittest.TestCase):
    ROOT = Path("/dev")

    def wt(self, name, merged="", pushed="origin/x", reopen=False, eligible=True):
        conv = hogs.Conversation("s-1", "Relatório mensal", f"/dev/shop-api/{name}", time.time() - 86400)
        w = hogs.Worktree(self.ROOT / "shop-api" / name, Path("/dev/shop-api"), "abc", f"refs/heads/feat/{name}",
                          1_500_000_000, time.time() - 40 * 86400, eligible=eligible, merged=merged, pushed=pushed)
        w.conversations = [hogs.ConvLink(conv, "dentro", reopen)]
        w.checks = [(True, "tudo commitado e enviado"), (False, "sem conferir processos")]
        return w

    def test_one_item_per_safe_worktree(self):
        rows = [self.wt("a", merged="squash em origin/main"), self.wt("b", reopen=True),
                self.wt("c", eligible=False)]
        items = hogs.worktree_items(rows, self.ROOT)
        self.assertEqual([i.label for i in items], ["shop-api / shop-api/a", "shop-api / shop-api/b"])
        self.assertTrue(all(i.risk is hogs.SAFE and i.cat == "worktree" for i in items))
        self.assertEqual(items[0].hint, "já na main (squash em origin/main)")
        self.assertTrue(items[1].hint.startswith("enviado (origin/x), parado há 40 d"))
        self.assertTrue(items[1].hint.endswith("⚠ reabra antes de apagar"))
        with mock.patch.object(hogs, "remove_worktree") as rm:
            items[1].action()
        rm.assert_called_once_with(rows[1], 0, self.ROOT)

    def test_tui_items_replace_the_grouped_worktree_item(self):
        rows = [self.wt("a", merged="squash em origin/main"), self.wt("b")]
        s = hogs.Snapshot(1, 1, hogs.Docker(), [hogs.worktree_item(rows, self.ROOT)], [], worktrees=rows,
                          worktrees_root=self.ROOT)
        labels = [i.label for i in hogs.tui_items(s)]
        self.assertEqual(labels, ["shop-api / shop-api/a", "shop-api / shop-api/b"])

    def test_apply_items_reports_guard_failure_and_success(self):
        done, seen = [], []
        ok = hogs.Item("dev", "npm", 10, hogs.SAFE, lambda: done.append("npm"))
        guarded = hogs.Item("apps", "Cópias antigas do Chrome (2)", 10, hogs.SAFE, lambda: done.append("x"),
                            guard=lambda: "feche o Chrome antes")
        failing = hogs.Item("docker", "Imagem old:t1", 10, hogs.LOSS,
                            lambda: (_ for _ in ()).throw(RuntimeError("Error: conflict: image is in use")))
        freed = hogs.apply_items([ok, guarded, failing], lambda i, good, msg: seen.append((i.label, good, msg)))
        self.assertEqual(done, ["npm"])
        self.assertEqual(seen, [("npm", True, ""), ("Cópias antigas do Chrome (2)", False, "pulei, feche o Chrome antes"),
                                ("Imagem old:t1", False, "Error: conflict: image is in use")])
        self.assertGreaterEqual(freed, 0)

    def test_detail_has_full_text_checks_and_every_conversation(self):
        w = self.wt("relatorio", reopen=True)
        w.conversations.append(hogs.ConvLink(hogs.Conversation("s-2", "Antiga", "/x", time.time() - 9 * 86400),
                                             "citou", False))
        text = "\n".join(str(t) for t in hogs.worktree_detail(w, self.ROOT))
        for expected in ("shop-api/relatorio · feat/relatorio", "✓ tudo commitado e enviado",
                         "✗ sem conferir processos", "a pasta sai, a branch fica", "“Relatório mensal”",
                         "cd /dev/shop-api/relatorio && claude --resume s-1", "⚠ reabra antes de apagar",
                         "“Antiga”", "claude --resume s-2"):
            self.assertIn(expected, text)
        self.assertLess(text.index("Relatório mensal"), text.index("Antiga"))
        long = hogs.Item("apps", "Parado: ~/Documents/" + "x" * 200, 10, hogs.EVAL, path=Path("/tmp/x"))
        lines = "\n".join(str(t) for t in hogs.item_detail(long, "feche o app antes"))
        self.assertIn("x" * 200, lines)
        self.assertIn("para avaliar: não volta sozinho", lines)
        self.assertIn("✗ feche o app antes", lines)
```

Migrar também `DockerCleanItemsTest.test_failed_removal_is_reported_not_marked_done`: continua passando sem mudar (o `clean()` imprime as mesmas linhas).

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.TuiItemsTest` → FAIL.
- [ ] **Step 3:** Implementar; os campos novos de `Item` vão por último, com padrão `None`, para nenhum construtor posicional existente mudar.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): um item por worktree, apply_items e texto completo do 🔎`.

---

### Task 4: Desenho (linhas ANSI, painel de lista, pilha e mapa de cliques)

**Files:**
- Modify: `hogs.py` (seção `tela: desenho`)
- Test: `tests/test_hogs.py` (`# ---- Task 4`: `RenderTest`)

**Interfaces:**
- Consumes: Task 2 (`Column`, `Row`, `Visible`, `ListState`, `mark_box`, `checkable`).
- Produces:
  - `MIN_W, MIN_H = 60, 20`; `LIST_TOP = 3` (borda, cabeçalho e régua: a primeira linha de item fica 3 linhas abaixo do topo do painel); `MARK_X = 6`; `TOO_SMALL = "Janela pequena demais: aumente para pelo menos 60×20."`.
  - `render_lines(renderable: RenderableType, width: int, color_system: str | None = None) -> list[str]`: imprime num `Console(file=io.StringIO(), width=width, force_terminal=color_system is not None, color_system=color_system, legacy_windows=False)` e devolve as linhas (sem o `\n` final).
  - `list_panel(title: str, columns: list[Column], vis: list[Visible], state: ListState, marked: set[str], height: int, border: str = "green", empty: str = "") -> Panel`:
    - painel de altura exata `height`, `padding=(0, 1)`, com uma `Table` de `box.SIMPLE_HEAD`, `expand=True`, `show_edge=False`, `pad_edge=False`, em que **toda** coluna é `no_wrap=True, overflow="ellipsis"`;
    - primeira coluna: a caixinha (`mark_box`, largura 3); na coluna do rótulo, recuo de `"  " * depth` e `"▾ "`/`"▸ "` nos grupos;
    - linhas `vis[scroll : scroll + height - 4]`; a linha do cursor com estilo `"reverse"`; as linhas sem célula numa coluna ficam vazias nela;
    - lista vazia: a linha `empty` (cinza) no lugar das linhas.
  - `detail_panel(title: str, lines: list, width: int, max_height: int) -> Panel`: texto com quebra de linha (nada cortado). Se passar de `max_height`, as linhas que cabem e uma última `"… mais N linhas: aumente a janela"`.
  - `tab_bar(names: list[str], active: int) -> tuple[Text, list[tuple[int, int, int]]]`: `" 1 ✨ Recomendado │ 2 🌳 Worktrees │ …"`, com a aba ativa em `"bold reverse"`, mais `(x0, x1, índice)` de cada aba, em células (`rich.cells.cell_len`).
  - `class Flex(NamedTuple): name: str; build: Callable[[int], RenderableType]; first: int; count: int` (o painel de lista, montado com a altura que sobrar).
  - `class Frame(NamedTuple): lines: list[str]; rows: dict[int, tuple[str, int]]; tabs: list[tuple[int, int, int]]`.
  - `stack(width: int, height: int, parts: list, color_system: str | None = None, tabs: list[tuple[int, int, int]] | None = None) -> Frame`:
    - `parts` tem renderables e exatamente um `Flex`; cada renderable vira linhas por `render_lines`;
    - o `Flex` recebe `h = height − soma das outras`; com `width < MIN_W`, `height < MIN_H` ou `h < 6`, devolve um `Frame` só com `TOO_SMALL` (centrado) e mapas vazios;
    - `rows[y] = (flex.name, flex.first + k)`, para `k` de 0 até `min(count − first, h − 4) − 1` e `y = topo do flex + LIST_TOP + k`;
    - `lines` sempre com exatamente `height` linhas (completa com `""` ou corta).

- [ ] **Step 1: Write the failing tests**

```python
class RenderTest(unittest.TestCase):
    COLS = [hogs.Column("O quê", ratio=4), hogs.Column("Libera", "right"), hogs.Column("Risco"),
            hogs.Column("Por quê", ratio=3)]

    def rows(self, n=30):
        out = []
        for k in range(n):
            label = f"🗂  ação/{k} " + "caminho-muito-longo/" * 10
            i = hogs.Item("apps", label, k * 1e9, hogs.SAFE, lambda: None)
            out.append(hogs.Row(hogs.item_key(i), [label, hogs.fmt(i.size), "🟢", "por quê " * 20], i))
        return out

    def frame(self, width=100, height=30, state=None, rows=None):
        rows = self.rows() if rows is None else rows
        vis = hogs.visible_rows(rows, set())
        st = state or hogs.ListState()
        flex = hogs.Flex("rec", lambda h: hogs.list_panel("✨ Recomendado agora", self.COLS, vis, st, set(), h,
                                                          empty="Nada recomendado agora. 🎉"), st.scroll, len(vis))
        return hogs.stack(width, height, [hogs.Text("topo"), flex, hogs.Text("rodapé")])

    def test_exact_height_and_one_line_per_row(self):
        f = self.frame()
        self.assertEqual(len(f.lines), 30)
        widths = {hogs.cell_len(f.lines[y]) for y in f.rows}
        self.assertEqual(len(widths), 1, "todas as linhas da lista com a mesma largura")
        for y, (name, idx) in f.rows.items():
            self.assertEqual(name, "rec")
            self.assertIn(f"ação/{idx} ", f.lines[y])

    def test_rows_map_follows_scroll(self):
        st = hogs.ListState(cursor=20, scroll=15)
        f = self.frame(state=st)
        first = min(f.rows)
        self.assertEqual(f.rows[first], ("rec", 15))
        self.assertIn("ação/15 ", f.lines[first])

    def test_too_small_and_empty(self):
        for w, h in ((40, 30), (100, 10)):
            f = self.frame(w, h)
            self.assertEqual(len(f.lines), h)
            self.assertIn("Janela pequena demais", "\n".join(f.lines))
            self.assertEqual(f.rows, {})
        f = self.frame(rows=[])
        self.assertIn("Nada recomendado agora", "\n".join(f.lines))
        self.assertEqual(f.rows, {})

    def test_detail_panel_wraps_and_caps(self):
        lines = [hogs.Text("x" * 250)] + [hogs.Text(f"linha {k}") for k in range(30)]
        out = "\n".join(hogs.render_lines(hogs.detail_panel("🔎 Por que é seguro", lines, 100, 40), 100))
        self.assertEqual(out.count("x"), 250)
        capped = hogs.render_lines(hogs.detail_panel("🔎", lines, 100, 8), 100)
        self.assertEqual(len(capped), 8)
        self.assertIn("mais", capped[-2])

    def test_tab_bar_ranges(self):
        text, ranges = hogs.tab_bar(["✨ Recomendado", "🌳 Worktrees", "🐳 Docker", "💾 Disco"], 1)
        self.assertEqual([r[2] for r in ranges], [0, 1, 2, 3])
        plain = text.plain
        for x0, x1, idx in ranges:
            self.assertLess(x0, x1)
        self.assertIn("2 🌳 Worktrees", plain)
```

(`hogs.cell_len` é `rich.cells.cell_len`, importado no esqueleto ou nesta tarefa.)

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.RenderTest` → FAIL.
- [ ] **Step 3:** Implementar. A tabela usa as colunas recebidas, precedidas pela coluna da caixinha (`width=3`); `ratio` e `width` passam para `add_column`.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): painéis em linhas, lista com altura fixa e mapa de cliques`.

---

### Task 5: Abas Recomendado e Disco

**Files:**
- Modify: `hogs.py`: `header()` ganha `compact: bool = False` (`padding=(0, 1)` e sem a linha em branco entre o texto e a barra); seção `tela: abas Recomendado e Disco`.
- Test: `tests/test_hogs.py` (`# ---- Task 5`: `RecommendedTreeTest`, `DiskTreeTest`)

**Interfaces:**
- Consumes: Task 2 (`Row`, `Column`, `item_key`), Task 3 (`item_detail`, `tui_items`), `CAT_TITLES`, `trim_trapped`, `TRIM_OFF_NOTE`, `RECENT_ROLLBACK`, `Snapshot.advice`, `Snapshot.others`, `Snapshot.system`, `GB`.
- Produces:
  - `REC_COLUMNS = [Column("O quê", ratio=4), Column("Libera", "right"), Column("Risco"), Column("Por quê", ratio=3)]`; `DISK_COLUMNS = [Column("O quê", ratio=4), Column("Tamanho", "right"), Column("Risco"), Column("Detalhe", ratio=3)]`.
  - `item_row(i: Item, guards: dict[str, str | None]) -> Row`: `Row(item_key(i), [i.label, fmt(i.size), i.risk.icon, Text(guard, style="yellow") if guard else i.hint], i, detail=item_detail(i, guard), title=i.label, path=i.path, resume=<conversa mais recente de i.worktree, se houver>, group=f"disk/{i.cat}")`, com `guard = guards.get(item_key(i))`.
  - `run_guards(items: list[Item]) -> dict[str, str | None]`: chama cada `guard` uma vez (só de itens com `guard`).
  - `recommended_tree(s: Snapshot, items: list[Item], trim_off: bool, guards: dict[str, str | None]) -> tuple[list[Column], list[Row]]`, nesta ordem:
    - grupos 🟢 por `cat`, ordenados pelo que liberam. Chave `f"rec/{cat}"`, `kind="group"`, células `[CAT_TITLES[cat] (para worktree: f"🌳 Worktrees que podem sair ({n})"), fmt(total) ou "—" se trim_trapped, "🟢", TRIM_OFF_NOTE se trim_trapped senão ""]`, com `limit=5` no grupo de worktrees e `detail` = resumo (quantos, total, "a branch fica" nos worktrees);
    - as ações ↻ de `s.advice`: `Row(f"rec/advice/{n}", [f"💻 {label}", f"~{fmt(size)}" if size else "", Text("↻", style="cyan"), how], kind="advice", how=how, detail=[label, how, "o hogs nunca reinicia nem atualiza o Mac"])`;
    - os grupos 🟡 `EVAL`: `"rec/eval/pessoal"` ("🗂  Arquivos parados para avaliar (n)", "vão para a Lixeira") e `"rec/eval/ponto_de_volta"` ("🐳 Pontos de volta recentes (n)", `RECENT_ROLLBACK`), com filhos `item_row`;
    - se `not s.mounts_known`, uma `Row("rec/note/mounts", [Text("Docker não respondeu: não sei quais pastas os containers usam, então não sugiro arquivos.", style="yellow")], kind="info")`.
  - `disk_tree(s: Snapshot, items: list[Item], trim_off: bool, guards: dict[str, str | None]) -> tuple[list[Column], list[Row]]`: grupos `disk/docker`, `disk/dev`, `disk/apps`, `disk/worktree`, `disk/system`, `disk/others` (títulos `CAT_TITLES`, `"💻 macOS e swap"` e `"🔎 Outros grandes"`). Células: `[título, fmt(total), "", plural(n, "item", "itens")]`. Filhos:
    - todos os `items` daquela `cat`, por tamanho (`item_row`);
    - os `s.items` sem `action` daquela `cat`, com tamanho ≥ `MIN_SHOW`, como `kind="info"` (`[label, fmt(size), "⚪", hint]`);
    - em `disk/system`: as linhas de `s.system`, se houver (swap, volumes APFS e tempo ligado, como `info`), e as ações ↻;
    - em `disk/others`: `s.others` com ≥ 1 GB, como `info`.

    Grupos vazios não aparecem.

- [ ] **Step 1: Write the failing tests**

```python
class RecommendedTreeTest(unittest.TestCase):
    def snap(self, items, advice=(), mounts_known=True):
        return hogs.Snapshot(500 * hogs.GB, 28 * hogs.GB, hogs.Docker(running=True), [], [], advice=list(advice),
                             mounts_known=mounts_known)

    def items(self):
        act = lambda: None
        return [hogs.Item("docker", "Docker: cache de build", 40e9, hogs.SAFE, act, "próximo build fica mais lento"),
                hogs.Item("apps", "Cópias antigas do Chrome (3)", 8e9, hogs.SAFE, act, "versões 1, 2",
                          guard=lambda: "feche o Chrome antes"),
                hogs.Item("worktree", "shop-api / shop-api/a", 1e9, hogs.SAFE, act, "já na main"),
                hogs.Item("apps", "Parado: ~/Downloads/video.mov", 16e9, hogs.EVAL, act, "vai para a Lixeira",
                          kind="pessoal"),
                hogs.Item("docker", "Imagem acme/api:dev", 3e9, hogs.LOSS, act, "construída aqui"),
                hogs.Item("dev", "Maven (~/.m2)", 2e9, hogs.REDOWN, act, "baixa no próximo build")]

    def test_order_groups_advice_and_eval(self):
        items = self.items()
        cols, rows = hogs.recommended_tree(self.snap(items, [("Reiniciar o Mac", 20e9, "Menu  › Reiniciar")]),
                                           items, False, hogs.run_guards(items))
        self.assertEqual(cols, hogs.REC_COLUMNS)
        self.assertEqual([r.key for r in rows], ["rec/docker", "rec/apps", "rec/worktree", "rec/advice/0",
                                                 "rec/eval/pessoal"])
        flat = {r.key for r in rows} | {c.key for r in rows for c in r.children}
        self.assertNotIn("docker|Imagem acme/api:dev", flat, "🔴 nunca no Recomendado")
        self.assertNotIn("dev|Maven (~/.m2)", flat, "🟡 baixa de novo só na aba Disco")
        chrome = rows[1].children[0]
        self.assertEqual(str(chrome.cells[3]), "feche o Chrome antes")
        self.assertEqual(rows[3].kind, "advice")
        self.assertFalse(hogs.checkable(rows[3]))
        self.assertEqual(rows[2].limit, 5)

    def test_trim_off_shows_dash_and_note(self):
        items = self.items()
        _, rows = hogs.recommended_tree(self.snap(items), items, True, {})
        docker = rows[[r.key for r in rows].index("rec/docker")]
        self.assertEqual((str(docker.cells[1]), str(docker.cells[3])), ("—", hogs.TRIM_OFF_NOTE))

    def test_empty_and_mounts_unknown(self):
        self.assertEqual(hogs.recommended_tree(self.snap([]), [], False, {})[1], [])
        _, rows = hogs.recommended_tree(self.snap([], mounts_known=False), [], False, {})
        self.assertEqual([r.key for r in rows], ["rec/note/mounts"])


class DiskTreeTest(unittest.TestCase):
    def test_every_risk_by_block_plus_info(self):
        items = RecommendedTreeTest.items(None)
        s = hogs.Snapshot(500 * hogs.GB, 28 * hogs.GB, hogs.Docker(running=True),
                          [hogs.Item("apps", "WhatsApp", 3e9, hogs.INFO, None, "Ajustes › Armazenamento")],
                          [("~/Projetos-antigos", 5 * hogs.GB), ("~/pequeno", 1e8)])
        cols, rows = hogs.disk_tree(s, items, False, {})
        self.assertEqual(cols, hogs.DISK_COLUMNS)
        by = {r.key: r for r in rows}
        self.assertEqual(set(by), {"disk/docker", "disk/dev", "disk/apps", "disk/worktree", "disk/others"})
        self.assertIn("docker|Imagem acme/api:dev", {c.key for c in by["disk/docker"].children})
        whats = [c for c in by["disk/apps"].children if c.kind == "info"]
        self.assertEqual([str(c.cells[0]) for c in whats], ["WhatsApp"])
        self.assertEqual([str(c.cells[0]) for c in by["disk/others"].children], ["~/Projetos-antigos"])
        self.assertTrue(all(c.group == "disk/docker" for c in by["disk/docker"].children if c.item))
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.RecommendedTreeTest tests.test_hogs.DiskTreeTest` → FAIL.
- [ ] **Step 3:** Implementar `REC_COLUMNS`, `DISK_COLUMNS`, `run_guards`, `item_row`, `recommended_tree`, `disk_tree` e `header(…, compact)`.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): abas Recomendado e Disco como árvores de linhas`.

---

### Task 6: Aba Worktrees

**Files:**
- Modify: `hogs.py` (seção `tela: aba Worktrees`)
- Test: `tests/test_hogs.py` (`# ---- Task 6`: `WorktreesTreeTest`)

**Interfaces:**
- Consumes: Task 2, Task 3 (`worktree_items`, `worktree_detail`, `item_key`), `verdict_counts`, `VERDICTS`, `VERDICT_LABELS`, `where`, `branch_name`, `elapsed`, `claude_dir`.
- Produces:
  - `WT_FILTERS = ["", "seguro", "depois", "manter"]` e `WT_FILTER_LABELS = {"": "todos", "seguro": "🟢 podem sair", "depois": "🟡 em andamento", "manter": "⚪ com trabalho só aqui"}`.
  - `worktrees_tree(rows: list[Worktree], root: Path | None, days: int = 0, flt: str = "", conversations: bool | None = None) -> tuple[list[Column], list[Row]]`:
    - colunas `[Column("Worktree · branch", ratio=4), Column("Tamanho", "right"), Column("Mexido"), Column("Na main?", ratio=2), Column("Em uso por", ratio=2), Column("Conversa", ratio=3), Column("Veredito", ratio=2)]`; sem a coluna Conversa quando `conversations` é falso (padrão: `claude_dir().is_dir()`);
    - um grupo por projeto (`w.repo`), por nome sem diferenciar maiúsculas. Chave `f"wt/{w.repo}"`, `limit=8`, células `[f"📁 {repo.name}", fmt(total), "", "", "", ("",) …, f"🟢{a} 🟡{b} ⚪{c}"]`, filhos ordenados por veredito (seguro, depois, manter) e tamanho;
    - linha de cada worktree: `key` = `item_key` do item de `worktree_items` (`Row.item` = esse item) para os 🟢; `f"wt/{w.path}"` e sem item para os outros. Células:
      - `where(w, root)` + `" · "` + branch (cinza);
      - `fmt(size)`;
      - `elapsed(last_activity)` ou `"—"`;
      - na main: `✓ {merged}`, `✗ sem merge · ✓ enviado`, ou `✗ não comprovado`;
      - em uso: `", ".join(users)` ou `"—"`;
      - conversa: `"● "` se ao vivo + título + `" · "` + elapsed, ou `"—"`;
      - veredito: `f"{icon} {VERDICT_LABELS[v]}"` na cor do veredito.

      `detail=worktree_detail(w, root)`, `title=f"{w.repo.name} / {where(w, root)}"`, `path=w.path`, `resume` = conversa mais recente;
    - `flt` não vazio mostra só os worktrees desse veredito, e projetos sem nenhum somem.
  - `worktree_totals(rows: list[Worktree], flt: str = "") -> Text`: `"🟢 4 podem sair · 1,2 GB   🟡 22 em andamento · 8,0 GB   ⚪ 44 com trabalho só aqui · 20,0 GB"` (de `verdict_counts`) + `f"   filtro: {WT_FILTER_LABELS[flt]}"` se `flt`.

- [ ] **Step 1: Write the failing tests**

```python
class WorktreesTreeTest(unittest.TestCase):
    ROOT = Path("/dev")

    def rows(self):
        def wt(repo, name, eligible=False, waits=(), blockers=(), merged=""):
            w = hogs.Worktree(self.ROOT / repo / name, self.ROOT / repo, "abc", f"refs/heads/feat/{name}", 1e9,
                              time.time() - 3 * 86400, eligible=eligible, merged=merged,
                              waits=list(waits), blockers=list(blockers))
            return w
        out = [wt("shop-api", "a", eligible=True, merged="squash em origin/main"),
               wt("shop-api", "b", waits=["mexido há 3 d"]), wt("shop-api", "c", blockers=["commit não enviado"])]
        out += [wt("acme", f"x{k}", eligible=True, merged="origin/main") for k in range(10)]
        out[1].conversations = [hogs.ConvLink(hogs.Conversation("s-9", "Checkout", "/dev/shop-api/b",
                                                                time.time() - 3600, live=True), "dentro", True)]
        return out

    def test_projects_sorted_with_limit_and_columns(self):
        cols, rows = hogs.worktrees_tree(self.rows(), self.ROOT, conversations=True)
        self.assertEqual([c.title for c in cols][5], "Conversa")
        self.assertEqual([r.key for r in rows], ["wt//dev/acme", "wt//dev/shop-api"])
        self.assertEqual(rows[0].limit, 8)
        shop = rows[1].children
        self.assertEqual([hogs.checkable(r) for r in shop], [True, False, False])
        self.assertEqual(shop[0].key, hogs.item_key(hogs.worktree_items(self.rows()[:1], self.ROOT)[0]))
        self.assertIn("● Checkout", str(shop[1].cells[5]))
        self.assertEqual(shop[1].resume.session, "s-9")
        self.assertIn("🟡 em andamento", str(shop[1].cells[6]))

    def test_filter_and_no_conversation_column(self):
        cols, rows = hogs.worktrees_tree(self.rows(), self.ROOT, flt="manter", conversations=False)
        self.assertNotIn("Conversa", [c.title for c in cols])
        self.assertEqual([r.key for r in rows], ["wt//dev/shop-api"])
        self.assertEqual(len(rows[0].children), 1)

    def test_totals_and_empty(self):
        text = hogs.worktree_totals(self.rows(), "seguro").plain
        self.assertIn("🟢 11 podem sair", text)
        self.assertIn("🟡 1 em andamento", text)
        self.assertIn("filtro: 🟢 podem sair", text)
        self.assertEqual(hogs.worktrees_tree([], self.ROOT, conversations=True)[1], [])
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.WorktreesTreeTest` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): aba Worktrees com todos os worktrees por projeto, filtro e conversa`.

---

### Task 7: Aba Docker

**Files:**
- Modify: `hogs.py`:
  - `Docker` ganha `containers: list[dict] = field(default_factory=list)`, um registro por container: `{"name", "image", "running": bool, "mounts": list[str]}`;
  - `gather_docker` preenche `containers`, com as origens de bind de cada container. O `docker inspect --format {{json .Mounts}}` devolve uma linha por ID, na ordem; se o número de linhas não bate com o de containers, `mounts` fica `[]`;
  - `docker_clean_items` passa `kind=` nos itens: `"cache"` (cache de build e de builder), `"sem_tag"`, `"container"` (parados, antigos e um a um), `"imagem"` (cada imagem e "Imagens pequenas"). Os pontos de volta continuam `"ponto_de_volta"`;
  - seção `tela: aba Docker`.
- Test: `tests/test_hogs.py` (`# ---- Task 7`: `DockerTreeTest`)

**Interfaces:**
- Consumes: Task 2, Task 5 (`item_row`), `rollback_info`, `ORIGIN_LABEL`, `docker_trim_off` (o valor vem por parâmetro), `short`.
- Produces:
  - `DOCKER_FILTERS = ["", "em_uso", "sem_uso", "local"]`; `DOCKER_FILTER_LABELS = {"": "todas", "em_uso": "em uso", "sem_uso": "sem uso", "local": "construídas aqui"}`.
  - `image_users(img: dict, containers: list[dict]) -> list[dict]`: containers cujo `image` é `img["name"]`, `img["ref"]` ou começa com `img["id"]`.
  - `docker_tree(d: Docker, items: list[Item], guards: dict[str, str | None], flt: str = "") -> tuple[list[Column], list[Row]]`:
    - colunas `[Column("O quê", ratio=4), Column("Tamanho", "right"), Column("Risco"), Column("Detalhe", ratio=4)]`;
    - `d.running` falso: `[]`;
    - grupos, nesta ordem e só se não vazios:
      - `"dk/build"` 🧱 Cache e sobras de build (itens `cache` e `sem_tag`);
      - `"dk/rollback"` ⏪ Pontos de volta de QA (itens `ponto_de_volta`, um por linha, com o `hint` de idade e situação);
      - `"dk/stopped"` 📦 Containers parados (itens `container`);
      - `"dk/images"` 🖼 Imagens: **todas** as imagens com tag, por tamanho, menos os pontos de volta (já estão no grupo deles; a mesma chave não pode aparecer duas vezes). A linha é o `item_row` se a imagem tem item `"imagem"` próprio (rótulo `f"Imagem {name}"`); senão é `info`, com risco ⚪ se usada e a célula Detalhe `f"{ORIGIN_LABEL[origin]} · {usuários com '● ' se running} · dados em {short(m)}"` (só as partes que existem). Quando a imagem tem item, o Detalhe é esse mesmo texto em vez do `hint`. O filtro `flt` se aplica aqui: `em_uso` = `used`; `sem_uso` = não `used`; `local` = `origin == "local"`;
      - `"dk/protected"` 🛡 Containers rodando e volumes (nunca apagados): `info` de cada `d.live` (`● nome`, com dados montados) e de cada volume (`"volume nome"`, tamanho, `"em uso"`/`"sem uso: revise à mão"`).
  - `docker_totals(d: Docker, items: list[Item], trim_off: bool, flt: str = "") -> Text`: `f"Docker.raw {fmt(d.raw_real)}"` (+ `f" de {fmt(d.raw_limit)}"`) + `"   🟢 X · 🟡 Y · 🔴 Z · ⚪ em uso W"` (somas de `items` por risco; `W` = imagens usadas + volumes) + `TRIM_OFF_NOTE` se `trim_off` + `f"   filtro: {DOCKER_FILTER_LABELS[flt]}"` se `flt`. Com o Docker parado: `"Docker parado: abra o Docker Desktop pra ver o detalhe"`.

- [ ] **Step 1: Write the failing tests**

```python
class DockerTreeTest(unittest.TestCase):
    def docker(self):
        d = hogs.Docker(running=True, raw_real=92e9, raw_limit=200e9, build_cache=40e9)
        img = lambda name, used, origin, size: {"repo": name.split(":")[0], "tag": name.split(":")[1], "id": f"{size:.0f}"[:6],
                                                "name": name, "ref": name, "size": size, "used": used,
                                                "origin": origin, "created": "3 days ago", "labels": {}}
        d.images = [img("mcr.example/mssql:2022", True, "registry", 3e9), img("acme/api:latest", False, "local", 2e9),
                    img("acme/api:antes-20260101-1200", False, "local", 1e9)]
        d.containers = [{"name": "sqlserver", "image": "mcr.example/mssql:2022", "running": True,
                         "mounts": ["/Users/x/dados/mssql"]}]
        d.live = ["sqlserver"]
        d.volumes = [{"name": "pgdata", "size": 5e9, "used": True}]
        return d

    def test_groups_rollbacks_images_and_protected(self):
        d = self.docker()
        with mock.patch.object(hogs, "first_seen", return_value={"acme/api:antes-20260101-1200": 0}):
            items = hogs.docker_clean_items(d)
            cols, rows = hogs.docker_tree(d, items, {})
        self.assertEqual([r.key for r in rows], ["dk/build", "dk/rollback", "dk/images", "dk/protected"])
        rb = rows[1].children[0]
        self.assertEqual(rb.key, "docker|Ponto de volta acme/api:antes-20260101-1200")
        images = {str(c.cells[0]): c for c in rows[2].children}
        sql = images["mcr.example/mssql:2022"]
        self.assertEqual(sql.kind, "info")
        self.assertIn("● sqlserver", str(sql.cells[3]))
        self.assertIn("dados em /Users/x/dados/mssql", str(sql.cells[3]))
        self.assertTrue(hogs.checkable(images["Imagem acme/api:latest"]))
        self.assertTrue(all(not hogs.checkable(c) for c in rows[3].children), "protegidos nunca marcáveis")

    def test_filter_and_stopped_docker(self):
        d = self.docker()
        with mock.patch.object(hogs, "first_seen", return_value={"acme/api:antes-20260101-1200": 0}):
            _, rows = hogs.docker_tree(d, hogs.docker_clean_items(d), {}, flt="em_uso")
        images = next(r for r in rows if r.key == "dk/images")
        self.assertEqual([str(c.cells[0]) for c in images.children], ["mcr.example/mssql:2022"])
        self.assertEqual(hogs.docker_tree(hogs.Docker(), [], {})[1], [])
        self.assertIn("Docker parado", hogs.docker_totals(hogs.Docker(), [], False).plain)

    def test_gather_keeps_container_mounts_in_order(self):
        # usar o fake_docker de DockerCleanItemsTest/MountsTest com dois containers e duas linhas de inspect
        ...
```

O último teste usa o mesmo `fake_docker` de `MountsTest`: dois containers, com o inspect devolvendo duas linhas `[{"Type": "bind", "Source": …}]`. Ele afirma que `d.containers[i]["mounts"]` segue a ordem dos IDs, e que, com uma linha só, os dois ficam com `mounts == []`.

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.DockerTreeTest` → FAIL.
- [ ] **Step 3:** Implementar. Os testes de `DockerCleanItemsTest` e `MountsTest` continuam passando.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): aba Docker com cache, pontos de volta, todas as imagens e protegidos`.

---

### Task 8: App (abas, teclas, mouse, confirmação e execução)

**Files:**
- Modify: `hogs.py` (seção `tela: app`)
- Test: `tests/test_hogs.py` (`# ---- Task 8`: `AppTest`)

**Interfaces:**
- Consumes: Tasks 1–7, `header`, `protected_panel`, `worktrees_line`, `docker_trim_off`, `apply_items`, `resume_command`, `DATA_VOLUME`.
- Produces:
  - `TABS = ["✨ Recomendado", "🌳 Worktrees", "🐳 Docker", "💾 Disco"]`.
  - `copy_text(text: str) -> bool` (`subprocess.run(["pbcopy"], input=text.encode(), timeout=5)`; `OSError`/timeout → `False`); `reveal(path: Path) -> bool` (`sh(["open", "-R", str(path)])`).
  - `class App`:
    - `__init__(self, s: Snapshot | None, records: list[dict], worktrees: list[Worktree], root: Path | None, days: int = 0, tab: int = 0, color_system: str | None = None, trim_off: bool | None = None)`. Com `s`: `items = tui_items(s)`, `guards = run_guards(items)` e `trim_off` (o padrão vem de `docker_trim_off()`). Sem `s` (modo `--worktrees`): `items = worktree_items(worktrees, root, days)` se houver `root`. Uma `ListState` por aba, com os grupos de projeto da aba Worktrees já abertos. Atributos públicos `tab`, `states`, `marked`, `mode` (`"list" | "help" | "confirm" | "done"`), `status`, `log`, `last: Frame | None`.
    - `trees(self) -> list[tuple[list[Column], list[Row]]]`: uma por aba, a partir de `items` sem os já removidos (`self.gone`). Sem `s`, as abas 0, 2 e 3 são `([], [])`.
    - `frame(self, width: int, height: int) -> Frame` (guarda em `self.last`). Linha 0: `tab_bar`. Depois, conforme a aba:
      - **Recomendado:** `header(s, records, items, trim_off, compact=True)`, `Flex` da lista "✨ Recomendado agora", `detail_panel("🔎 Por que é seguro · <título>", …)`, `protected_panel(s)`;
      - **Worktrees:** `worktree_totals`, `Flex` "🌳 Worktrees", detalhe;
      - **Docker:** `docker_totals`, `Flex` "🐳 Docker", detalhe;
      - **Disco:** `header(…, compact=True)`, `Flex` "💾 Disco", detalhe.

      A última linha é o rodapé: `f"Marcados: {fmt(total)} ({n})"`, mais as teclas, mais `status`, cortado na largura. O detalhe ocupa até `max(5, height // 3)` linhas. Lista vazia com texto `empty`:
      - Recomendado: `"Nada recomendado agora. 🎉"`;
      - Worktrees sem `root`: o texto de `worktrees_line([], None)`;
      - Worktrees com `root`: `"Nenhum worktree em <root>"`;
      - Docker: `"Docker parado: abra o Docker Desktop pra ver o detalhe"`;
      - abas sem `s`: `f"Esta abertura é só dos worktrees de {short(root)}: rode hogs para o disco todo."`.

      Com `mode == "help"`, a lista dá lugar ao painel de ajuda. Com `"confirm"`, ao painel "🗑 Vou apagar":
      - cada marcado: rótulo, tamanho e risco;
      - `"Total: X"`;
      - `"Atenção: tem item 🔴 na lista, ele não volta."` se houver `LOSS`;
      - `"⚠ reabra antes de apagar “título”: <resume_command>"` de cada conversa com `reopen`;
      - `"Arquivos vão para a Lixeira."` se houver `kind == "pessoal"`;
      - `"s apaga · n ou Esc volta"`.

      Com `"done"`, ao painel com `log` (`✔ rótulo` / `↷ rótulo: motivo`), `"Liberado: X · livre agora: Y"`, a nota do Docker.raw se algum item `docker` saiu, e `"números da última medição; rode hogs de novo para medir · qualquer tecla volta"`.
    - `handle(self, key: Key, height: int = 40) -> bool` (`False` = sair). `height` é a altura da lista no último quadro (`self.list_height`, guardada pelo `frame`). Teclas:
      - em `"list"`:
        - ↑ ↓ pgup pgdn home end (rodinha = ±3) movem; → ← chamam `open_row` / `close_row`;
        - espaço chama `toggle` na linha em foco; `a` chama `mark_all_safe` na árvore da aba, com o status `"marquei N itens 🟢"`;
        - `1`–`4`, tab e backtab trocam de aba; `f` cicla `WT_FILTERS` (aba Worktrees) ou `DOCKER_FILTERS` (aba Docker);
        - `e` vai para a aba Disco com `row.group` aberto e o cursor nele (sem `group`, o status diz `"nada para explorar aqui"`);
        - `c` chama `copy_text(resume_command(row.resume) if row.resume else str(row.path))`, com o status `"copiado: …"`; `o` chama `reveal(row.path)`; `r` é tratado em `run` (aqui, sem conversa, o status diz `"esta linha não tem conversa do Claude Code"`);
        - Enter: com marcados, vai para `"confirm"`; sem marcados, numa linha `advice` põe `row.how` no status; senão o status diz `"marque com espaço (a marca todos 🟢) e tecle Enter"`;
        - `?` vai para `"help"`; `q`, Esc e ctrl_c saem;
        - click: com `y == 0`, troca para a aba de `last.tabs`; com `y in last.rows`, move o cursor até lá e, se `x < MARK_X`, chama `toggle`;
      - em `"help"`: qualquer tecla volta para `"list"`;
      - em `"confirm"`: `s` chama `execute()`; `n` e Esc voltam;
      - em `"done"`: qualquer tecla volta;
      - em qualquer modo, ctrl_c sai.
    - `execute(self, draw: Callable[[], None] = lambda: None) -> None`: roda `apply_items(marcados, notify)`. O `notify` acrescenta a `log` e chama `draw()`. Os ✔ vão para `self.gone` e saem de `marked`; os ↷ continuam marcados, com o motivo no `status`. Depois `mode = "done"`, com `self.freed` = o retorno.
    - `run(self, term: Terminal) -> None`: laço:
      - `term.size()`, depois `term.draw(self.frame(w, h).lines)`;
      - `term.read(0.25)` e, para cada tecla, `r` com conversa é tratado aqui: `term.suspended()` e então `subprocess.run(["claude", "--resume", conv.session], cwd=conv.start)`, ou o status `"a pasta da conversa não existe mais; use c para copiar o comando"`; as outras teclas vão para `handle`;
      - em `"confirm"`, o `s` chama `execute(draw)` com um `draw` que redesenha;
      - sai quando `handle` devolve `False`.
  - `run_tui(app: App, mouse: bool) -> None`: `with Terminal(mouse) as term: app.run(term)`.

- [ ] **Step 1: Write the failing tests** (nomes e afirmações; a montagem usa um `Snapshot` fictício com os itens de `RecommendedTreeTest.items`, `advice` e 3 worktrees de `WorktreesTreeTest.rows()[:3]`, `trim_off=False`, `color_system=None`; `run_guards` com `process_running` mockado)

```python
class AppTest(unittest.TestCase):
    # app() monta o App descrito acima; press(app, *names) chama handle(Key(n)) para cada nome
    def test_opens_on_recommended_with_nothing_marked(self):
        # frame(120, 40): linha 0 contém "1 ✨ Recomendado"; "✨ Recomendado agora"; "🔎 Por que é seguro"; "🛡 Não mexo";
        # rodapé contém "Marcados: 0 B (0)"; app.marked == set()

    def test_a_marks_only_safe_and_enter_confirms_with_total_and_red_warning(self):
        # press "a" → marked contém só itens SAFE; nunca o EVAL nem o LOSS
        # move até a linha do 🔴 na aba Disco ("4", → no grupo docker), espaço → Enter → frame tem "Vou apagar",
        # "Total:", "Atenção: tem item 🔴"; press "n" → mode "list", nada executado

    def test_execute_reports_done_and_skipped_and_removes_done_rows(self):
        # itens com action que registra a chamada e um com guard que devolve "feche o Chrome antes"
        # marca os dois, Enter, "s" → log tem "✔ Docker: cache de build" e "↷ Cópias antigas do Chrome (3): pulei, feche o Chrome antes"
        # depois, a linha do cache some de trees()[0]; a do Chrome continua; frame tem "Liberado:"

    def test_reopen_warning_in_confirm(self):
        # worktree 🟢 com ConvLink(reopen=True), marcado → frame em "confirm" contém "⚠ reabra antes de apagar “"
        # e o resume_command

    def test_tabs_by_number_tab_and_click(self):
        # "2" → tab 1; "tab" → 2; "backtab" → 1; frame; click em (x dentro do range da aba 4, y=0) → tab 3

    def test_click_on_row_moves_cursor_and_on_box_toggles(self):
        # frame(120, 40); y = uma chave de last.rows com índice 2; click (x=20, y) → states[0].cursor == 2, nada marcado;
        # click (x=2, y) → marcado o item dessa linha (se marcável)

    def test_e_jumps_to_disk_group_and_f_filters(self):
        # cursor num item docker do Recomendado; "e" → tab 3, "disk/docker" em states[3].expanded, cursor nessa linha
        # aba Worktrees: "f" → states[1].flt == "seguro"; frame tem "filtro: 🟢 podem sair"

    def test_advice_enter_shows_how_and_is_never_marked(self):
        # cursor na linha ↻ → espaço não marca; Enter → status == how

    def test_c_and_o_and_r_without_conversation(self):
        # mock copy_text / reveal; numa linha de worktree com conversa → copy_text chamado com o resume_command;
        # "o" → reveal(w.path); numa linha sem conversa "r" → status "esta linha não tem conversa do Claude Code"

    def test_quit_keys_and_small_window_and_empty(self):
        # handle(Key("q")) is False; Key("ctrl_c") is False em qualquer modo; Key("esc") em help volta para list
        # frame(40, 10) tem "Janela pequena demais"; App sem nada (Snapshot vazio, sem root): cada aba mostra seu texto
        # de vazio e "down", "space", "enter", "a", "e", "c" não levantam

    def test_worktrees_only_mode(self):
        # App(None, [], rows, root, tab=1): aba 0 mostra "Esta abertura é só dos worktrees de"; aba 1 lista os projetos

    def test_run_loop_draws_and_exits_on_q(self):
        # term fake: size() → (120, 40); read() devolve [Key("down")], depois [Key("q")]; draw guarda as linhas
        # run(term) termina; draw chamado ao menos 2 vezes
```

O implementador escreve cada teste com os nomes e afirmações acima, e todos usam dados fictícios.

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.AppTest` → FAIL.
- [ ] **Step 3:** Implementar `TABS`, `copy_text`, `reveal`, `App` e `run_tui`.
- [ ] **Step 4:** PASS; suíte → `OK`.
- [ ] **Step 5:** Commit `feat(tela): app com abas, teclado, mouse, confirmação e execução`.

---

### Task 9: Linha de comando, fim do fluxo numerado, docs, screenshots e 0.6.0

**Files:**
- Modify: `hogs.py`:
  - `run()`;
  - textos que citam o `--clean` numerado (cabeçalho do módulo e `epilog`, `worktrees_summary` "Apagar os 🟢: … --clean", `stopped_rows` "veja um por um no --clean", `recommended_panel` "um por um no hogs --clean", rodapé do `report`, docstrings);
  - remover `clean`, `clean_worktrees`, `parse_selection` e `selection_numbers`, mais o `Prompt`/`Confirm` se ficarem sem uso;
  - `__version__`.
- Modify: `pyproject.toml` (0.6.0), `README.md`, `CLAUDE.md`, `docs/make_screenshots.py`, SVGs em `docs/`.
- Test: `tests/test_hogs.py` (`# ---- Task 9`: `CliTest`; remover ou migrar `SelectionTest`, `test_selection_numbers`, `test_failed_removal_is_reported_not_marked_done` (vira teste de `apply_items` com `docker_clean_items`), `test_clean_offers_only_safe_confirms_and_keeps_branch` (vira teste de `worktree_items` + `apply_items` com o repositório Git real do `WorktreeTest`: worktree removido, branch preservada), `test_clean_without_safe_offers_nothing` (vira: `worktree_items` vazio) e `test_clean_warns_to_reopen_before_confirm` (já coberto pelo `AppTest.test_reopen_warning_in_confirm`; remover)).

**Interfaces:**
- Consumes: `App`, `run_tui`, `report`, `worktrees_report`, `gather`, `gather_worktrees`, `history_load`, `record_snapshot`.
- Produces:
  - `interactive() -> bool`: `sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb")`.
  - Flags novas: `--report` ("imprime o relatório estático, mesmo num terminal") e `--no-mouse` ("a tela sem mouse"). `--clean` muda o help para "abre a tela para escolher o que apagar (o mesmo que hogs)".
  - `run()`:
    - **`--worktrees PASTA`:** `remember_worktrees_root`, depois `gather_worktrees`. Com tela, `run_tui(App(None, [], rows, root, args.worktree_days, tab=1, color_system=Console().color_system), not args.no_mouse)`, com os `warnings` no `status` inicial. Sem tela (`--report` ou sem TTY), `worktrees_report` como hoje; se `--clean` veio junto, depois imprime `"A escolha do que apagar precisa de um terminal interativo."` e sai com código 2.
    - **Sem `--worktrees`:** `gather`, `history_load` e `record_snapshot`, como hoje. Com tela, `run_tui(App(snapshot, records, snapshot.worktrees, snapshot.worktrees_root, color_system=…), not args.no_mouse)`. Sem tela, `report(...)`; com `--clean`, mais a mesma mensagem e código 2.
  - Rodapé do relatório estático: `"Rode hogs num terminal para escolher o que apagar."`. No `worktrees_summary`, o passo de apagar vira `f"{cmd}"` (abre a tela na aba Worktrees). Nenhum texto de interface pode citar o fluxo numerado (números, `a = todos`, `--clean` como seleção).
  - `CLAUDE.md`:
    - "Every deletion goes through the selection + `Confirm` flow" vira: toda remoção passa por marcar → Enter → tela de confirmação → `s` (`App`), com reconferência por item (`apply_items`: guarda de novo e ↷ na falha); nada vem marcado ao abrir, e `a` marca só 🟢;
    - o bullet dos worktrees no relatório principal ganha: "a tela usa um `Item` por worktree (`worktree_items`), cada um removido por `remove_worktree`";
    - novo: a tela nunca abre sem TTY e restaura o terminal em qualquer saída; ↻ e ⚪ nunca têm caixinha; a aba Docker mostra containers vivos e volumes só como informação.
  - `README.md`:
    - tabela de uso: `hogs` (tela), `hogs --report`, `hogs --worktrees PASTA` (tela na aba Worktrees; com `--report`, o quadro), `--no-mouse`, `--clean` (o mesmo que `hogs`);
    - seção "Tela interativa" com as abas e as teclas: ↑↓ mover, →← abrir, espaço marcar, `a` todos 🟢, Enter apagar, `c` copiar, `o` Finder, `r` conversa, `e` explorar, `f` filtro, `1`–`4`/Tab abas, `?` ajuda, `q` sair; mouse: clique foca, clique na caixinha marca, rodinha rola, Option+arrastar seleciona texto no iTerm2;
    - garantias atualizadas;
    - screenshot novo.
  - `docs/make_screenshots.py`: `docs/tui.svg` (aba Recomendado depois de um `a`, cursor num worktree 🟢 enviado e parado, 120×40) e `docs/tui-worktrees.svg` (aba Worktrees, 120×40), a partir de `App(...).frame(...)` com `color_system="truecolor"`. Cada linha é impressa por `Text.from_ansi` num `Console(record=True, width=120)` e exportada com `export_svg`. Os SVGs atuais continuam (o relatório estático existe). Só nomes fictícios; `claude_retention`, `first_seen`, `process_running` e `docker_trim_off` mockados.

- [ ] **Step 1: Write the failing tests**

```python
class CliTest(unittest.TestCase):
    def run_cli(self, *argv, tty=True):
        with mock.patch.object(sys, "argv", ["hogs", *argv]), mock.patch.object(hogs, "interactive", return_value=tty), \
                mock.patch.object(hogs, "gather", return_value=hogs.Snapshot(1, 1, hogs.Docker(), [], [])), \
                mock.patch.object(hogs, "record_snapshot"), mock.patch.object(hogs, "history_load", return_value=[]), \
                mock.patch.object(hogs, "run_tui") as tui, mock.patch.object(hogs, "report") as rep:
            try:
                hogs.run()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return tui, rep, code

    def test_terminal_opens_the_screen_and_report_flag_forces_static(self):
        tui, rep, _ = self.run_cli()
        tui.assert_called_once()
        self.assertTrue(tui.call_args[0][1], "mouse ligado por padrão")
        rep.assert_not_called()
        tui, rep, _ = self.run_cli("--report")
        tui.assert_not_called()
        rep.assert_called_once()
        tui, _, _ = self.run_cli("--no-mouse")
        self.assertFalse(tui.call_args[0][1])

    def test_without_tty_prints_static_and_clean_explains(self):
        tui, rep, code = self.run_cli(tty=False)
        tui.assert_not_called()
        rep.assert_called_once()
        self.assertEqual(code, 0)
        tui, rep, code = self.run_cli("--clean", tty=False)
        tui.assert_not_called()
        self.assertEqual(code, 2)

    def test_worktrees_opens_on_worktrees_tab_without_measuring_disk(self):
        # tmp com uma pasta; gather_worktrees mockado → ([], []); run_cli("--worktrees", tmp):
        # run_tui chamado com App de tab == 1 e s is None; gather NÃO chamado; com "--report", worktrees_report chamado

    def test_interactive_needs_both_ttys_and_a_real_term(self):
        # mock isatty de stdin/stdout e TERM: só True com os dois TTY e TERM fora de ("", "dumb")

    def test_no_ui_text_mentions_the_numbered_flow(self):
        src = Path(hogs.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Quais apagar?", src)
        self.assertNotIn("um por um no hogs --clean", src)
        self.assertFalse(hasattr(hogs, "clean_worktrees"))
        self.assertFalse(hasattr(hogs, "parse_selection"))
```

- [ ] **Step 2:** Run `python3 -m unittest tests.test_hogs.CliTest` → FAIL.
- [ ] **Step 3:** Implementar `interactive()`, `run()` e a limpeza do fluxo numerado; migrar os testes listados em Files.
- [ ] **Step 4:** Suíte inteira → `OK` (3.9 e 3.12 no CI).
- [ ] **Step 5:** `python3 docs/make_screenshots.py`; conferir que os SVGs só têm nomes fictícios (`grep` pelos nomes de `docs/make_screenshots.py`); atualizar `README.md` e `CLAUDE.md`; versão 0.6.0 em `hogs.py` e `pyproject.toml`.
- [ ] **Step 6:** Commit `feat: hogs abre a tela interativa; --report, --no-mouse, docs e 0.6.0`.
