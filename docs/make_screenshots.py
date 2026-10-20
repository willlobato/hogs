"""Gera as imagens do README com dados fictícios (nada da sua máquina aparece).

    python3 docs/make_screenshots.py
"""
import io
import sys
import time
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

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
)


def item(cat, label, size, risk, hint="", action=noop):
    return h.Item(cat, label, int(size), risk, action if risk is not h.INFO else None, hint)


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

snapshot = h.Snapshot(total=int(494.4 * GB), free=int(18.2 * GB), docker=docker, items=items,
                      others=[("~/Development", int(15.4 * GB)), ("~/.vscode", int(6.1 * GB))], apps=apps)


# Uma semana de histórico: Docker estável e um dia ruim de builds (+0,7 GB por hora nas últimas 26h).
NOW = time.time()


def grew(hours):
    return 0.7 * min(hours, 26)


records = [{"t": NOW - hours * 3600, "free": int((18.2 + grew(hours)) * GB),
            "docker_raw": int((112.7 - grew(hours)) * GB)} for hours in range(168, 0, -1)]
base_sizes = h.snapshot_sizes(snapshot)
base_sizes.update({"Docker.raw": int(94.5 * GB), "Docker: imagens": int(28 * GB), "Docker: cache de build": int(29 * GB),
                   "WhatsApp": int(30.1 * GB), "npm": int(3.1 * GB), "~/Development": int(14.6 * GB)})
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


with mock.patch.object(h, "load_config", return_value={}), \
        mock.patch.object(h, "read_json", side_effect=lambda path: FAKE_JSON.get(path, {})):
    save("report.svg", lambda con: h.report(con, snapshot, records))
save("apps.svg", lambda con: (con.print(h.apps_table(apps, 7)), con.print(h.apps_footer())))
