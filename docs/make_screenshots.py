"""Gera as imagens do README com dados fictícios (nada da sua máquina aparece).

    python3 docs/make_screenshots.py
"""
import io
import sys
import tempfile
import time
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import hogs as h  # noqa: E402

GB, MB = 1_000_000_000, 1_000_000
noop = lambda: None  # noqa: E731


def image(name, size, used=False, origin=None, created="3 months ago"):
    repo = name.split(":")[0]
    return {"name": name, "repo": repo, "tag": name.split(":")[-1], "id": name[:12], "size": int(size),
            "used": used, "origin": origin or h.image_origin(repo), "created": created}


docker = h.Docker(
    running=True, raw_real=int(112.7 * GB), raw_limit=int(274.9 * GB), total=int(86 * GB),
    build_cache=int(21.7 * GB),
    images=[
        image("api:latest", 1.4 * GB, used=True, created="10 hours ago"),
        image("shop-api:latest", 1.1 * GB, used=True, created="2 days ago"),
        image("shop-api:antes-20260101-0900", 1.2 * GB, created="10 months ago"),
        image("postgres:16-alpine", 0.38 * GB, used=True),
        image("registry.gitlab.com/acme/browser-runner:latest", 6.8 * GB, created="4 years ago"),
        image("ollama/ollama:latest", 5.6 * GB, created="15 months ago"),
        image("ghcr.io/acme/worker:2.3.1", 4.3 * GB, created="2 years ago"),
        image("ml-experiment:t2", 10.4 * GB),
        image("scraper:dev", 3.6 * GB),
        image("node:18-alpine", 0.13 * GB, created="2 years ago"),
        image("<none>", 0.75 * GB, origin="none"),
    ],
    stopped=[{"name": f"selenium-{n}", "size": 25 * MB, "age": 400, "project": None, "project_running": False}
             for n in range(6)],
    volumes=[{"name": "pgdata", "size": int(15.9 * GB), "used": True},
             {"name": "old-cache", "size": int(1.5 * GB), "used": False}],
    builders=[{"name": "multiarch", "driver": "docker-container", "nodes": ["multiarch0"], "running": True}],
    builder_caches=[{"builder": "multiarch", "volume": "buildx_buildkit_multiarch0_state", "size": int(15.8 * GB)}],
    current_builder="multiarch",
    live=["api", "postgres", "shop-api"],
)


def item(cat, label, size, risk, hint="", action=noop, kind=""):
    return h.Item(cat, label, int(size), risk, action if risk is not h.INFO else None, hint, kind=kind)


items = [
    item("dev", "npm", 4.2 * GB, h.SAFE),
    item("dev", "JetBrains (caches)", 3.2 * GB, h.SAFE, "a IDE reindexa ao abrir"),
    item("dev", "Maven (~/.m2)", 3.6 * GB, h.REDOWN, "baixa no próximo build"),
    item("dev", "Playwright (navegadores)", 2.8 * GB, h.REDOWN, "npx playwright install"),
    item("dev", "Ollama (modelos)", 4.9 * GB, h.INFO, "ollama list → ollama rm <modelo>"),
    item("dev", "Node (nvm)", 1.4 * GB, h.INFO, "nvm uninstall <versão>"),
    item("apps", "Aplicativos (/Applications)", 40.3 * GB, h.INFO),
    item("apps", "WhatsApp", 31.7 * GB, h.INFO, "WhatsApp › Ajustes › Armazenamento › Gerenciar"),
    item("apps", "Chrome", 9.8 * GB, h.INFO, "perfis e cache do navegador"),
    item("apps", "Lixeira", 2.1 * GB, h.SAFE),
    item("apps", "Cópias antigas do Navegador (3)", 6.4 * GB, h.SAFE, "versões 120.0, 121.0; a instalada é 122.0"),
    item("apps", "Parado: ~/Downloads/curso-gravado.mov", 4.1 * GB, h.EVAL, "sem abrir há 14 meses · vai para a Lixeira",
         kind="pessoal"),
]

D, H = 86400, 3600
DEV, T0 = Path("~/Development"), time.time()


def wt(repo, name, branch, gb, age, merged="", users=(), sessions=(), blockers=(), waits=(), ok=False,
       pushed="", conversations=(), leftovers=(), hard=()):
    w = h.Worktree(DEV / name, DEV / repo, branch=f"refs/heads/{branch}", size=int(gb * GB),
                   last_activity=T0 - age, eligible=ok, merged=merged, pushed=pushed, users=list(users),
                   sessions=list(sessions), blockers=list(blockers), waits=list(waits),
                   conversations=list(conversations), leftovers=list(leftovers), hard=list(hard))
    w.reason = (f"{merged} · mexido {h.elapsed(w.last_activity)}" if ok and merged
                else f"enviado a {pushed} · mexido {h.elapsed(w.last_activity)} · a branch fica" if ok
                else f"{merged} · {len(w.leftovers)} fora do Git vão para a Lixeira: {h.leftover_text(w.leftovers)}"
                if w.trashable else (w.blockers or w.waits)[0])
    return w


def conv(session, title, start, age, live=False, how="dentro", reopen=False):
    c = h.Conversation(session, title, start, last=T0 - age, cwds={start}, live=live)
    return h.ConvLink(c, how, reopen)


WORKTREES = [
    wt("app", "app-fix-login", "fix/login", 0.9, 21 * D, "merge em origin/main", ok=True),
    wt("app", "app-old-header", "fix/header", 0.4, 12 * D, "squash em origin/main", ok=True),
    wt("app", "app-preview", "feat/preview", 0.8, 5 * H, "squash em origin/main", ["Claude Code app-12"],
       ["app-12"], ok=True),
    wt("app", "app-dashboard", "feat/dashboard", 1.4, 2 * H, users=["Claude Code app-7", "node ×3"],
       sessions=["app-7"], blockers=["merge não comprovado em origin/main"],
       waits=["em uso agora: Claude Code app-7 aberta dentro da pasta, node ×3"],
       hard=["em uso agora: Claude Code app-7 aberta dentro da pasta, node ×3"]),
    wt("api", "api-infra", "feat/infra", 0.6, 9 * D, "merge em origin/main",
       leftovers=[".terraform/", ".env"]),
    wt("api", "api-cache", "feat/cache", 0.3, 3 * D, "rebase em origin/main", ["node ×2"],
       waits=["em uso agora: node ×2"], hard=["em uso agora: node ×2"]),
    # Caminho absoluto fictício: resume_command não expande "~".
    wt("shop-api", "shop-api-relatorio", "feat/relatorio", 1.2, 41 * D, pushed="origin/feat/relatorio", ok=True,
       conversations=[conv("3f2a9c1e", "Relatório mensal por cliente", "/Users/ana/Development/shop-api-relatorio",
                           42 * D, reopen=True)]),
    wt("shop-api", "shop-api-cupom", "feat/cupom", 0.5, 3 * D, pushed="origin/feat/cupom",
       waits=["sem merge, mexido há 3 d (em andamento; prazo 30 d)"],
       conversations=[conv("8b41d07a", "Cupom de desconto no checkout", "/Users/ana/Development/shop-api-cupom",
                           2 * H, live=True)]),
]

now = datetime.now(timezone.utc)
apps = [h.AppInfo(name, Path(where) / f"{name}.app", int(size), int(data), now - timedelta(days=days))
        for name, where, size, data, days in [
            ("Scanner Utility", "/Applications", 49 * MB, 0, 1160),
            ("Keynote", "/Applications", 550 * MB, 0, 760),
            ("VirtualBox", "/Applications", 263 * MB, 0, 580),
            ("Obsidian", "/Applications", 462 * MB, 68 * MB, 425),
            ("Azure Data Studio", "/Applications", 714 * MB, 0, 425),
            ("Spotify", "/Applications", 366 * MB, 699 * MB, 335),
            ("WebStorm", "~/Applications", 2.7 * GB, 0, 270),
        ]]

system = h.SystemInfo({"System": int(12.4 * GB), "Preboot": int(8.1 * GB), "VM": int(15 * GB), "Data": int(430 * GB)},
                      swap_used=int(13.8 * GB), uptime_days=23, update_pending=False)
snapshot = h.Snapshot(total=int(494.4 * GB), free=int(18.2 * GB), docker=docker, items=items,
                      others=[("~/Pictures", int(52.3 * GB)), ("~/Music", int(30.1 * GB)), ("~/Movies", int(15.4 * GB)),
                              ("~/.vscode", int(6.1 * GB))], apps=apps,
                      system=system, advice=h.system_advice(system), projects=int(78.4 * GB),
                      mounts=[Path("~/Development/shop-api/.data/postgres")],
                      worktrees=WORKTREES, worktrees_root=DEV)
snapshot.items.append(h.worktree_item(WORKTREES, DEV))


# Uma semana de histórico: Docker estável e um dia ruim de builds (+0,7 GB por hora nas últimas 26h).
NOW = time.time()


def grew(hours):
    return 0.7 * min(hours, 26)


records = [{"t": NOW - hours * 3600, "free": int((18.2 + grew(hours)) * GB),
            "docker_raw": int((112.7 - grew(hours)) * GB)} for hours in range(168, 0, -1)]
base_sizes = h.snapshot_sizes(snapshot)
base_sizes.update({"Docker.raw": int(94.5 * GB), "Docker: imagens": int(28 * GB), "Docker: cache de build": int(29 * GB),
                   "WhatsApp": int(30.1 * GB), "npm": int(3.1 * GB), "~/Movies": int(14.6 * GB)})
records.append({"t": NOW - 26 * 3600, "full": True, "free": int(36.4 * GB), "docker_raw": int(94.5 * GB),
                "sizes": base_sizes})
records.sort(key=lambda r: r["t"])

# Configuração fictícia pro painel de prevenção (sem ler o Docker nem o hogs da sua máquina).
FAKE_JSON = {h.DAEMON_JSON: {"builder": {"gc": {"enabled": True, "defaultKeepStorage": "20GB"}}},
             h.DOCKER_SETTINGS: {"DiskTRIM": True}}


def save(name, render):
    con = Console(record=True, width=118, force_terminal=True, color_system="truecolor", file=io.StringIO())
    render(con)
    con.save_svg(str(ROOT / "docs" / name), title="hogs")
    print("ok", name)


# Pontos de volta "vistos" há tempo, sem gravar no seen_tags.json da sua máquina.
# Retenção do Claude Code "não configurada", sem ler o ~/.claude da sua máquina.
RETENTION = (False, f"Conversas do Claude Code: apagadas depois de {h.CLAUDE_DEFAULT_DAYS} dias",
             f'para guardar mais, ponha "cleanupPeriodDays": {h.CLAUDE_KEEP_DAYS} em ~/.claude/settings.json '
             "(o hogs não altera)")
with mock.patch.object(h, "load_config", return_value={}), \
        mock.patch.object(h, "claude_retention", return_value=RETENTION), \
        mock.patch.object(h, "read_json", side_effect=lambda path: FAKE_JSON.get(path, {})), \
        mock.patch.object(h, "first_seen", side_effect=lambda refs, now: {r: now - 300 * 86400 for r in refs}):
    save("report.svg", lambda con: h.report(con, snapshot, records))
save("apps.svg", lambda con: (con.print(h.apps_table(apps, 7)), con.print(h.apps_footer())))
save("worktrees.svg", lambda con: h.worktrees_report(con, WORKTREES, DEV))


# A tela interativa: cada linha de App.frame() já vem em ANSI; o Console só a grava para virar SVG.
TUI_W, TUI_H = 120, 40


def save_tui(name, app):
    con = Console(record=True, width=TUI_W, force_terminal=True, color_system="truecolor", file=io.StringIO())
    for line in app.frame(TUI_W, TUI_H).lines:
        con.print(Text.from_ansi(line), no_wrap=True, overflow="crop")
    con.save_svg(str(ROOT / "docs" / name), title="hogs")
    print("ok", name)


def focus(app, group, key):
    """Abre o grupo `group` da aba atual e põe o cursor na linha `key`."""
    app.states[app.tab].expanded.add(group)
    vis = h.visible_rows(app.trees()[app.tab][1], app.states[app.tab].expanded)
    app.states[app.tab].cursor = [v.row.key for v in vis].index(key)


# Pasta de conversas fictícia (vazia), para a coluna Conversa aparecer sem ler o ~/.claude da sua máquina.
with tempfile.TemporaryDirectory() as fake_claude, \
        mock.patch.object(h, "load_config", return_value={}), \
        mock.patch.object(h, "claude_dir", return_value=Path(fake_claude)), \
        mock.patch.object(h, "claude_retention", return_value=RETENTION), \
        mock.patch.object(h, "read_json", side_effect=lambda path: FAKE_JSON.get(path, {})), \
        mock.patch.object(h, "first_seen", side_effect=lambda refs, now: {r: now - 300 * 86400 for r in refs}), \
        mock.patch.object(h, "process_running", return_value=False), \
        mock.patch.object(h, "docker_trim_off", return_value=False):
    app = h.App(snapshot, records, WORKTREES, DEV, color_system="truecolor")
    app.handle(h.Key("a"))
    focus(app, "rec/worktree", "worktree|shop-api / shop-api-relatorio")
    save_tui("tui.svg", app)
    app = h.App(None, [], WORKTREES, DEV, tab=1, color_system="truecolor")
    focus(app, f"wt/{DEV / 'shop-api'}", "worktree|shop-api / shop-api-relatorio")
    save_tui("tui-worktrees.svg", app)
