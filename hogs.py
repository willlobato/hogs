#!/usr/bin/env python3
"""hogs: mostra quem está comendo o disco do Mac (Docker incluso) e ajuda a limpar com segurança.

Uso:
  hogs                 relatório
  hogs --clean         escolhe o que apagar
  hogs --alert 30      avisa quando o espaço livre cair abaixo de 30 GB
  hogs --alert off     desliga o aviso
  hogs --alert         mostra se o aviso está ativo
  hogs --alert test    manda uma notificação de teste
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

__version__ = "0.1.0"

HOME = Path.home()
LIB = HOME / "Library"
APP_SUPPORT = LIB / "Application Support"
CONTAINERS = LIB / "Containers"
GROUP_CONTAINERS = LIB / "Group Containers"
CACHES = LIB / "Caches"
DATA_VOLUME = "/System/Volumes/Data" if os.path.ismount("/System/Volumes/Data") else "/"
DOCKER_CONTAINER = CONTAINERS / "com.docker.docker"
DOCKER_RAW = DOCKER_CONTAINER / "Data/vms/0/data/Docker.raw"

LABEL = "dev.hogs.alert"
PLIST = LIB / "LaunchAgents" / f"{LABEL}.plist"
STATE = APP_SUPPORT / "hogs" / "state.json"
LOG = LIB / "Logs" / "hogs.log"

GB = 1e9
MIN_SHOW = 50e6          # itens menores que isso não aparecem
OLD_CONTAINER_DAYS = 30


# ---------------------------------------------------------------- risco

@dataclass(frozen=True)
class Risk:
    icon: str
    label: str
    color: str
    rank: int


SAFE = Risk("🟢", "seguro", "green", 0)
REDOWN = Risk("🟡", "baixa de novo", "yellow", 1)
LOSS = Risk("🔴", "perde algo", "red", 2)
INFO = Risk("⚪", "só informação", "bright_black", 3)


@dataclass
class Item:
    cat: str                     # docker | dev | apps
    label: str
    size: int = 0
    risk: Risk = INFO
    action: Callable[[], object] | None = None
    hint: str = ""
    guard: Callable[[], str | None] | None = None


# ---------------------------------------------------------------- utilidades

def sh(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def du(path: Path) -> int:
    if not path.exists():
        return 0
    r = sh(["du", "-sk", str(path)], timeout=900)
    if not r or not r.stdout:
        return 0
    try:
        return int(r.stdout.split()[0]) * 1024
    except ValueError:
        return 0


def fmt(n: float) -> str:
    if n >= GB:
        s = f"{n / GB:.1f} GB"
    elif n >= 1e6:
        s = f"{n / 1e6:.0f} MB"
    else:
        s = f"{n / 1e3:.0f} kB"
    return s.replace(".", ",")


_UNITS = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def parse_size(s: str) -> int:
    """Tamanhos do Docker: '1.4GB', '77.65kB', '1e+03MB', '49.63GB (58%)'."""
    m = re.match(r"\s*([-+0-9.e]+)\s*([kKMGT]?B)", s or "")
    if not m:
        return 0
    return max(0, int(float(m.group(1)) * _UNITS[m.group(2)]))


def short(p: Path | str) -> str:
    return str(p).replace(str(HOME), "~", 1)


def process_running(name: str) -> bool:
    r = sh(["pgrep", "-x", name])
    return bool(r and r.returncode == 0)


def empty_dir(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


# ---------------------------------------------------------------- Docker

# Imagens oficiais do Docker Hub (sem "usuario/"). Fora dessa lista, um nome sem barra
# é tratado como construído aqui: errar pra esse lado só deixa de sugerir a limpeza.
OFFICIAL_IMAGES = set("""
alpine amazoncorretto amazonlinux bash buildpack-deps busybox caddy cassandra centos
clojure consul couchdb debian docker eclipse-temurin elasticsearch erlang elixir fedora
gcc ghost golang gradle groovy haproxy hello-world httpd ibm-semeru-runtimes influxdb
jenkins jetty kibana kong logstash mariadb maven memcached mongo mysql neo4j nats nginx
node openjdk oraclelinux percona perl php postgres python rabbitmq redis registry
rockylinux almalinux ruby rust sapmachine solr sonarqube swift telegraf tomcat traefik
ubuntu vault wordpress zookeeper adminer
""".split())


def image_origin(repo: str) -> str:
    if repo == "<none>":
        return "none"
    first = repo.split("/")[0]
    if "/" in repo and ("." in first or ":" in first or first == "localhost"):
        return "registry"
    if "/" in repo or repo in OFFICIAL_IMAGES:
        return "hub"
    return "local"


ORIGIN_LABEL = {"registry": "registry", "hub": "Docker Hub", "local": "construída aqui", "none": "sem tag"}


def _age_days(ts: str) -> int:
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 9999
    if dt.year < 2000:           # container criado e nunca iniciado
        return 9999
    return (datetime.now(timezone.utc) - dt).days


def _labels(s: str) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in (s or "").split(",") if "=" in kv)


@dataclass
class Docker:
    running: bool = False
    raw_real: int = 0
    raw_limit: int = 0
    total: int = 0
    build_cache: int = 0
    images: list[dict] = field(default_factory=list)
    stopped: list[dict] = field(default_factory=list)
    volumes: list[dict] = field(default_factory=list)


def gather_docker() -> Docker:
    d = Docker()
    try:
        st = DOCKER_RAW.stat()
        d.raw_real, d.raw_limit = st.st_blocks * 512, st.st_size
    except OSError:
        pass

    summary = sh(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
    if not summary or summary.returncode != 0:
        return d
    d.running = True
    images_total = images_reclaimable = 0
    for line in summary.stdout.splitlines():
        row = json.loads(line)
        d.total += parse_size(row.get("Size", ""))
        if row.get("Type") == "Build Cache":
            d.build_cache = parse_size(row.get("Reclaimable", ""))
        elif row.get("Type") == "Images":
            images_total = parse_size(row.get("Size", ""))
            images_reclaimable = parse_size(row.get("Reclaimable", ""))

    verbose = sh(["docker", "system", "df", "-v", "--format", "{{json .}}"], timeout=180)
    if not verbose or verbose.returncode != 0:
        return d
    v = json.loads(verbose.stdout)

    for img in v.get("Images") or []:
        try:
            used = int(img.get("Containers") or 0) > 0
        except ValueError:
            used = False
        repo, tag = img.get("Repository", "<none>"), img.get("Tag", "<none>")
        d.images.append({
            "repo": repo, "tag": tag, "id": img.get("ID", "").removeprefix("sha256:")[:12],
            "name": repo if tag == "<none>" else f"{repo}:{tag}",
            "size": parse_size(img.get("UniqueSize", "")), "used": used,
            "origin": image_origin(repo), "created": img.get("CreatedSince", ""),
        })
    # Com o containerd, a soma dos UniqueSize passa do total real (camadas contadas em dobro).
    # Escala cada grupo pra bater com os totais do `docker system df`.
    for used, target in ((False, images_reclaimable), (True, images_total - images_reclaimable)):
        group = [i for i in d.images if i["used"] == used]
        reported = sum(i["size"] for i in group)
        if reported > target > 0:
            for i in group:
                i["size"] = int(i["size"] * target / reported)

    containers = v.get("Containers") or []
    running_projects = {
        _labels(c.get("Labels", "")).get("com.docker.compose.project")
        for c in containers if c.get("State") == "running"
    } - {None}
    stopped = [c for c in containers if c.get("State") != "running"]
    finished: dict[str, str] = {}
    if stopped:
        r = sh(["docker", "inspect", "--format", "{{.Id}}|{{.State.FinishedAt}}"] + [c["ID"] for c in stopped])
        for line in (r.stdout.splitlines() if r else []):
            cid, _, ts = line.partition("|")
            finished[cid] = ts
    for c in stopped:
        project = _labels(c.get("Labels", "")).get("com.docker.compose.project")
        d.stopped.append({
            "name": c.get("Names", c["ID"][:12]), "image": c.get("Image", ""),
            "size": parse_size(c.get("Size", "")), "age": _age_days(finished.get(c["ID"], "")),
            "project": project, "project_running": project in running_projects,
        })

    for vol in v.get("Volumes") or []:
        d.volumes.append({
            "name": vol.get("Name", ""), "size": parse_size(vol.get("Size", "")),
            "used": vol.get("Links", "0") not in ("0", "N/A"),
        })
    return d


def docker_clean_items(d: Docker) -> list[Item]:
    if not d.running:
        return []
    items: list[Item] = []
    if d.build_cache:
        items.append(Item("docker", "Docker: cache de build", d.build_cache, SAFE,
                          lambda: sh(["docker", "builder", "prune", "-a", "-f"], timeout=900),
                          "próximo build fica mais lento"))

    untagged = [i for i in d.images if not i["used"] and i["origin"] == "none"]
    if untagged:
        ids = [i["id"] for i in untagged]
        items.append(Item("docker", f"Docker: imagens sem tag ({len(untagged)})", sum(i["size"] for i in untagged),
                          SAFE, lambda ids=ids: [sh(["docker", "rmi", x]) for x in ids], "versões antigas órfãs"))

    old = [c for c in d.stopped if c["age"] >= OLD_CONTAINER_DAYS and not c["project_running"]]
    if old:
        names = [c["name"] for c in old]
        preview = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
        items.append(Item("docker", f"Docker: containers parados há +{OLD_CONTAINER_DAYS} dias ({len(old)})",
                          sum(c["size"] for c in old), SAFE,
                          lambda names=names: [sh(["docker", "rm", n]) for n in names], preview))
    for c in d.stopped:
        if c in old:
            continue
        why = f"projeto {c['project']} está rodando" if c["project_running"] else f"parado há {c['age']} dias"
        items.append(Item("docker", f"Docker: container {c['name']}", c["size"],
                          REDOWN if c["project"] else LOSS,
                          lambda n=c["name"]: sh(["docker", "rm", n]), why))

    small_pullable = []
    for img in sorted(d.images, key=lambda i: -i["size"]):
        if img["used"] or img["origin"] == "none":
            continue
        local = img["origin"] == "local"
        if not local and img["size"] < 500e6:
            small_pullable.append(img)
            continue
        items.append(Item("docker", f"Imagem {img['name']}", img["size"], LOSS if local else REDOWN,
                          lambda n=img["name"]: sh(["docker", "rmi", n]),
                          f"{ORIGIN_LABEL[img['origin']]} · {img['created']}"))
    if small_pullable:
        names = [i["name"] for i in small_pullable]
        items.append(Item("docker", f"Imagens pequenas de registry/Hub ({len(names)})",
                          sum(i["size"] for i in small_pullable), REDOWN,
                          lambda names=names: [sh(["docker", "rmi", n]) for n in names],
                          ", ".join(n.rsplit("/", 1)[-1] for n in names[:3]) + "…"))
    return items


# ---------------------------------------------------------------- pastas conhecidas

@dataclass
class PathDef:
    cat: str
    label: str
    paths: list[Path]
    risk: Risk
    action: str | list[str] | None = None      # "rm" | "empty" | comando
    hint: str = ""
    guard_proc: str | None = None


PATH_DEFS = [
    # caches de dev
    PathDef("dev", "npm", [HOME / ".npm"], SAFE, ["npm", "cache", "clean", "--force"]),
    PathDef("dev", "Homebrew", [CACHES / "Homebrew"], SAFE, ["brew", "cleanup", "-s"]),
    PathDef("dev", "pip", [CACHES / "pip"], SAFE, "rm"),
    PathDef("dev", "Yarn", [CACHES / "Yarn"], SAFE, "rm"),
    PathDef("dev", "JetBrains (caches)", [CACHES / "JetBrains"], SAFE, "rm", "a IDE reindexa ao abrir"),
    PathDef("dev", "VS Code (atualizações)", [CACHES / "com.microsoft.VSCode.ShipIt"], SAFE, "rm"),
    PathDef("dev", "Xcode DerivedData", [LIB / "Developer/Xcode/DerivedData"], SAFE, "rm"),
    PathDef("dev", "Maven (~/.m2)", [HOME / ".m2/repository"], REDOWN, "rm", "baixa no próximo build"),
    PathDef("dev", "Gradle", [HOME / ".gradle/caches"], REDOWN, "rm", "baixa no próximo build"),
    PathDef("dev", "Playwright (navegadores)", [CACHES / "ms-playwright"], REDOWN, "rm", "npx playwright install"),
    PathDef("dev", "Go modules", [HOME / "go/pkg/mod"], REDOWN, ["go", "clean", "-modcache"]),
    PathDef("dev", "Cargo registry", [HOME / ".cargo/registry"], REDOWN, "rm"),
    PathDef("dev", "pnpm store", [LIB / "pnpm/store"], REDOWN, "rm"),
    PathDef("dev", "Ollama (modelos)", [HOME / ".ollama/models"], INFO, None, "ollama list → ollama rm <modelo>"),
    PathDef("dev", "Cache genérico (~/.cache)", [HOME / ".cache"], INFO, None, "confira as subpastas"),
    PathDef("dev", "Node (nvm)", [HOME / ".nvm/versions"], INFO, None, "nvm uninstall <versão>"),
    PathDef("dev", "Python (pyenv)", [HOME / ".pyenv/versions"], INFO, None, "pyenv uninstall <versão>"),
    PathDef("dev", "SDKMAN (Java, Maven…)", [HOME / ".sdkman/candidates"], INFO, None, "sdk uninstall <sdk> <versão>"),
    PathDef("dev", "Simuladores iOS", [LIB / "Developer/CoreSimulator"], INFO, None, "xcrun simctl delete unavailable"),
    # apps
    PathDef("apps", "WhatsApp", [GROUP_CONTAINERS / "group.net.whatsapp.WhatsApp.shared",
                                 CONTAINERS / "net.whatsapp.WhatsApp"], INFO, None,
            "WhatsApp › Ajustes › Armazenamento › Gerenciar"),
    PathDef("apps", "Claude (VM do Cowork)", [APP_SUPPORT / "Claude/vm_bundles"], REDOWN, "rm",
            "baixa de novo se usar o Cowork", guard_proc="Claude"),
    PathDef("apps", "Chrome", [APP_SUPPORT / "Google/Chrome"], INFO, None, "perfis e cache do navegador"),
    PathDef("apps", "Teams", [CONTAINERS / "com.microsoft.teams2"], INFO),
    PathDef("apps", "Backups do iPhone", [APP_SUPPORT / "MobileSync/Backup"], INFO, None,
            "Finder › iPhone › Gerenciar Backups"),
    PathDef("apps", "Lixeira", [HOME / ".Trash"], SAFE, "empty"),
    PathDef("apps", "Downloads", [HOME / "Downloads"], INFO, None, "revise arquivos antigos"),
    PathDef("apps", "Aplicativos (/Applications)", [Path("/Applications")], INFO),
]


def make_action(pd: PathDef) -> Callable[[], object] | None:
    if pd.action is None:
        return None
    if pd.action == "rm":
        return lambda: [shutil.rmtree(p, ignore_errors=True) for p in pd.paths]
    if pd.action == "empty":
        return lambda: empty_dir(pd.paths[0])
    return lambda: sh(pd.action, timeout=900)


def make_guard(pd: PathDef) -> Callable[[], str | None] | None:
    if not pd.guard_proc:
        return None
    return lambda: f"feche o {pd.guard_proc} antes" if process_running(pd.guard_proc) else None


# Pastas varridas pra achar o que não está na lista acima.
SCAN_ROOTS = [HOME, APP_SUPPORT, CONTAINERS, GROUP_CONTAINERS, CACHES]


def scan_candidates(known: set[str]) -> list[Path]:
    def covered(p: Path) -> bool:
        s = str(p)
        return any(s == k or k.startswith(s + "/") or s.startswith(k + "/") for k in known)

    out = []
    for root in SCAN_ROOTS:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for c in children:
            if c == LIB or c.is_symlink() or covered(c):
                continue
            out.append(c)
    return out


# ---------------------------------------------------------------- apps menos usados

APP_GLOBS = [Path("/Applications"), HOME / "Applications"]


@dataclass
class AppInfo:
    name: str
    path: Path
    size: int
    data_size: int                   # Containers, Application Support e Caches do app
    last_used: datetime | None


def _app_info(app: Path) -> AppInfo:
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        info = {}
    bundle_id = info.get("CFBundleIdentifier", "")
    name = info.get("CFBundleName") or app.stem

    # O macOS atualiza o atime do executável quando o app abre (o índice do Spotlight pode estar quebrado).
    stamps = []
    exe = app / "Contents/MacOS" / info.get("CFBundleExecutable", app.stem)
    try:
        stamps.append(exe.stat().st_atime)
    except OSError:
        pass
    try:
        stamps.append((LIB / "Saved Application State" / f"{bundle_id}.savedState").stat().st_mtime)
    except OSError:
        pass
    last = datetime.fromtimestamp(max(stamps), timezone.utc) if stamps else None

    data_paths = {APP_SUPPORT / name, APP_SUPPORT / app.stem}
    if bundle_id:
        data_paths |= {CONTAINERS / bundle_id, CACHES / bundle_id, APP_SUPPORT / bundle_id}
    return AppInfo(app.stem, app, du(app), sum(du(p) for p in data_paths), last)


def list_apps() -> list[Path]:
    apps = []
    for root in APP_GLOBS:
        for pattern in ("*.app", "*/*.app"):
            apps += [p for p in root.glob(pattern) if not p.is_symlink()]
    # desinstaladores e apps auxiliares ocultos só poluem a lista
    noise = re.compile(r"uninstall|desinstal", re.I)
    return sorted({p for p in apps if not p.name.startswith(".") and not noise.search(p.name)})


def gather_apps() -> list[AppInfo]:
    with ThreadPoolExecutor(max_workers=8) as ex:
        apps = list(ex.map(_app_info, list_apps()))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(apps, key=lambda a: a.last_used or epoch)


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "sem registro"
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return "hoje"
    if days < 2:
        return "ontem"
    if days < 60:
        return f"há {days} dias"
    if days < 365:
        return f"há {days // 30} meses"
    years, months = divmod(days // 30, 12)
    y = f"{years} ano{'s' if years > 1 else ''}"
    return f"há {y} e {months} {'mês' if months == 1 else 'meses'}" if months else f"há {y}"


def apps_table(apps: list[AppInfo], limit: int) -> Table | None:
    rows = apps[:limit]
    if not rows:
        return None
    top = max(a.size + a.data_size for a in rows) or 1
    t = Table(title=f"🗂  Apps menos usados (top {len(rows)})", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    t.add_column("App", ratio=3, no_wrap=True, overflow="ellipsis")
    t.add_column("Último uso", no_wrap=True)
    t.add_column("App", justify="right", no_wrap=True)
    t.add_column("+ Dados", justify="right", no_wrap=True)
    t.add_column("", no_wrap=True, width=14)
    t.add_column("Onde", style="dim", ratio=2, no_wrap=True, overflow="ellipsis")
    for a in rows:
        days = (datetime.now(timezone.utc) - a.last_used).days if a.last_used else 9999
        color = "red" if days >= 365 else "yellow" if days >= 90 else "default"
        total = a.size + a.data_size
        t.add_row(a.name, Text(ago(a.last_used), style=color), Text(fmt(a.size), style="bold"),
                  fmt(a.data_size) if a.data_size >= 1e6 else "—", mini_bar(total, top, color if color != "default" else "grey62"),
                  short(a.path.parent))
    return t


def apps_footer() -> Text:
    return Text("Pra remover: arraste o app pro Lixo. Os dados em ~/Library ficam pra trás; "
                "o AppCleaner (grátis) leva tudo junto.", style="dim")


# ---------------------------------------------------------------- coleta

@dataclass
class Snapshot:
    total: int
    free: int
    docker: Docker
    items: list[Item]               # dev + apps
    others: list[tuple[str, int]]
    apps: list[AppInfo] = field(default_factory=list)


def gather(con: Console) -> Snapshot:
    known = {str(p) for pd in PATH_DEFS for p in pd.paths} | {str(DOCKER_CONTAINER)}
    def_paths = [p for pd in PATH_DEFS for p in pd.paths]
    candidates = scan_candidates(known)
    paths = def_paths + candidates

    sizes: dict[Path, int] = {}
    with con.status("[bold]🐷 Farejando o disco…[/]", spinner="dots") as status:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut_docker = ex.submit(gather_docker)
            futs = {ex.submit(du, p): p for p in paths}
            for n, f in enumerate(as_completed(futs), 1):
                sizes[futs[f]] = f.result()
                status.update(f"[bold]🐷 Farejando o disco…[/] [dim]{n}/{len(paths)} pastas · Docker…[/]")
            status.update("[bold]🐷 Vendo quais apps você não abre há tempo…[/]")
            apps = gather_apps()
            status.update("[bold]🐷 Perguntando pro Docker…[/]")
            docker = fut_docker.result()

    items = [Item(pd.cat, pd.label, sum(sizes.get(p, 0) for p in pd.paths), pd.risk,
                  make_action(pd), pd.hint, make_guard(pd)) for pd in PATH_DEFS]
    others = sorted(((short(p), sizes[p]) for p in candidates if sizes.get(p, 0) >= GB), key=lambda x: -x[1])
    usage = shutil.disk_usage(DATA_VOLUME)
    return Snapshot(usage.total, usage.free, docker, items, others, apps)


# ---------------------------------------------------------------- relatório

def mini_bar(size: int, top: int, color: str, width: int = 14) -> Text:
    n = min(width, max(1 if size > 0 else 0, round(size / top * width))) if top else 0
    return Text("━" * n, style=color) + Text("━" * (width - n), style="grey23")


def risk_text(r: Risk) -> Text:
    return Text(f"{r.icon} {r.label}", style=r.color)


def category_table(title: str, rows: list[tuple[str, int, Risk, str]]) -> Table | None:
    rows = [r for r in rows if r[1] >= MIN_SHOW]
    if not rows:
        return None
    rows.sort(key=lambda r: -r[1])
    top = rows[0][1]
    t = Table(title=title, title_justify="left", title_style="bold", box=box.SIMPLE_HEAD,
              expand=True, show_edge=False, pad_edge=False)
    t.add_column("Item", ratio=3, no_wrap=True, overflow="ellipsis")
    t.add_column("Tamanho", justify="right", no_wrap=True)
    t.add_column("", no_wrap=True, width=14)
    t.add_column("Risco", no_wrap=True)
    t.add_column("Dica", style="dim", ratio=3, overflow="fold")
    for label, size, risk, hint in rows:
        t.add_row(label, Text(fmt(size), style="bold"), mini_bar(size, top, risk.color), risk_text(risk), hint)
    return t


def docker_rows(d: Docker) -> list[tuple[str, int, Risk, str]]:
    raw_hint = f"limite {fmt(d.raw_limit)} · Docker › Settings › Resources" if d.raw_limit else ""
    rows = [("Disco virtual (Docker.raw)", d.raw_real, INFO, raw_hint)]
    if not d.running:
        rows.append(("Docker parado", 0, INFO, "abra o Docker Desktop pra ver o detalhe"))
        return rows

    def group(pred):
        sel = [i for i in d.images if pred(i)]
        return sel, sum(i["size"] for i in sel)

    used, used_size = group(lambda i: i["used"])
    pull, pull_size = group(lambda i: not i["used"] and i["origin"] in ("registry", "hub"))
    local, local_size = group(lambda i: not i["used"] and i["origin"] == "local")
    none, none_size = group(lambda i: not i["used"] and i["origin"] == "none")
    vol_free = [v for v in d.volumes if not v["used"]]
    vol_used = [v for v in d.volumes if v["used"]]
    local_names = ", ".join(i["name"] for i in sorted(local, key=lambda i: -i["size"])[:3])

    rows += [
        (f"Imagens em uso ({len(used)})", used_size, INFO, "usadas por containers"),
        (f"Imagens sem uso, de registry ({len(pull)})", pull_size, REDOWN, "voltam com docker pull"),
        (f"Imagens sem uso, construídas aqui ({len(local)})", local_size, LOSS, local_names),
        (f"Imagens sem tag ({len(none)})", none_size, SAFE, "versões antigas órfãs"),
        ("Cache de build", d.build_cache, SAFE, "próximo build fica mais lento"),
        (f"Containers parados ({len(d.stopped)})", sum(c["size"] for c in d.stopped), SAFE, ""),
        (f"Volumes sem uso ({len(vol_free)})", sum(v["size"] for v in vol_free), INFO, "dados de bancos: revise à mão"),
        (f"Volumes em uso ({len(vol_used)})", sum(v["size"] for v in vol_used), INFO, "nunca mexo"),
    ]
    untracked = d.raw_real - d.total
    if untracked > 3 * GB:
        rows.append(("Não contabilizado pelo Docker", untracked, INFO, "VM e sobras; parte volta ao reiniciar o Docker"))
    return rows


def unused_images_table(d: Docker) -> Table | None:
    imgs = sorted((i for i in d.images if not i["used"] and i["origin"] != "none"), key=lambda i: -i["size"])[:8]
    if not imgs:
        return None
    t = Table(title="Maiores imagens sem uso", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    t.add_column("Imagem", ratio=4, no_wrap=True, overflow="ellipsis")
    t.add_column("Tamanho", justify="right", no_wrap=True)
    t.add_column("Origem", no_wrap=True)
    t.add_column("Criada", style="dim", no_wrap=True)
    for i in imgs:
        risk = LOSS if i["origin"] == "local" else REDOWN
        t.add_row(i["name"], Text(fmt(i["size"]), style="bold"),
                  Text(f"{risk.icon} {ORIGIN_LABEL[i['origin']]}", style=risk.color), i["created"])
    return t


def header(s: Snapshot) -> Panel:
    used = s.total - s.free
    dev = sum(i.size for i in s.items if i.cat == "dev")
    apps = sum(i.size for i in s.items if i.cat == "apps")
    docker = s.docker.raw_real
    rest = max(0, used - dev - apps - docker)
    segs = [("Docker", docker, "dodger_blue1"), ("Caches de dev", dev, "magenta"),
            ("Apps e dados", apps, "yellow"), ("Sistema e outros", rest, "grey50"), ("Livre", s.free, "green")]

    width = 64
    bar, cum, pos = Text(), 0, 0
    for _, size, color in segs:
        cum += size
        end = round(cum / s.total * width)
        bar.append("█" * (end - pos), style=color)
        pos = end
    legend = Text()
    for label, size, color in segs:
        legend.append("■ ", style=color).append(f"{label} ").append(fmt(size), style="bold").append("  ")

    free_color = "red" if s.free < 30 * GB else "yellow" if s.free < 60 * GB else "green"
    line = Text.assemble(("Macintosh HD", "bold"), f"  ·  {fmt(used)} de {fmt(s.total)} usados  ·  ",
                         (f"{fmt(s.free)} livres", f"bold {free_color}"))
    return Panel(Group(line, Text(), bar, legend), title="[bold]🐷 hogs[/]  [dim]quem está comendo seu disco[/]",
                 title_align="left", border_style="grey50", padding=(1, 2))


def report(con: Console, s: Snapshot) -> None:
    con.print(header(s))
    for t in (category_table("🐳 Docker", docker_rows(s.docker)), unused_images_table(s.docker),
              category_table("🧰 Caches de dev", [(i.label, i.size, i.risk, i.hint) for i in s.items if i.cat == "dev"]),
              category_table("📦 Apps e dados", [(i.label, i.size, i.risk, i.hint) for i in s.items if i.cat == "apps"]),
              category_table("🔎 Outros grandes (fora da lista acima)",
                             [(label, size, INFO, "") for label, size in s.others[:12]])):
        if t is not None:
            con.print(t)
            con.print()
    if (t := apps_table(s.apps, 10)) is not None:
        con.print(t)
        con.print(Text("Lista maior: hogs --apps 20", style="dim"))
        con.print()

    cleanable = clean_items(s)
    safe = sum(i.size for i in cleanable if i.risk is SAFE)
    redown = sum(i.size for i in cleanable if i.risk is REDOWN)
    con.print(Panel(Text.assemble(
        "Dá pra liberar  ", (f"🟢 {fmt(safe)} sem risco", "bold green"), "  ·  ",
        (f"🟡 {fmt(redown)} que baixam de novo", "bold yellow"), "\n",
        ("Rode ", "dim"), ("hogs --clean", "bold cyan"), (" pra escolher o que apagar.", "dim")),
        border_style="green", padding=(0, 2)))


# ---------------------------------------------------------------- limpeza

def clean_items(s: Snapshot) -> list[Item]:
    items = docker_clean_items(s.docker) + [i for i in s.items if i.action]
    items = [i for i in items if i.size >= 1e6]
    return sorted(items, key=lambda i: (i.risk.rank, -i.size))


def parse_selection(raw: str, items: list[Item]) -> list[Item]:
    chosen: set[int] = set()
    for tok in re.split(r"[,\s]+", raw.strip().lower()):
        if not tok:
            continue
        if tok == "a":
            chosen |= {n for n, i in enumerate(items, 1) if i.risk is SAFE}
        elif m := re.fullmatch(r"(\d+)-(\d+)", tok):
            chosen |= set(range(int(m.group(1)), int(m.group(2)) + 1))
        elif tok.isdigit():
            chosen.add(int(tok))
    return [items[n - 1] for n in sorted(chosen) if 1 <= n <= len(items)]


def clean(con: Console, s: Snapshot) -> None:
    items = clean_items(s)
    if not items:
        con.print("[green]Nada pra limpar. 🎉[/]")
        return

    t = Table(title="🧹 O que dá pra apagar", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    t.add_column("#", justify="right", style="cyan", no_wrap=True)
    t.add_column("Item", ratio=4, no_wrap=True, overflow="ellipsis")
    t.add_column("Tamanho", justify="right", no_wrap=True)
    t.add_column("Risco", no_wrap=True)
    t.add_column("Detalhe", style="dim", ratio=3, overflow="ellipsis", no_wrap=True)
    for n, i in enumerate(items, 1):
        t.add_row(str(n), i.label, Text(fmt(i.size), style="bold"), risk_text(i.risk), i.hint)
    con.print(t)
    con.print("[dim]🟢 seguro · 🟡 volta com download/rebuild · 🔴 perde algo (só se tiver certeza). "
              "Containers rodando e volumes nunca aparecem aqui.[/]\n")

    raw = Prompt.ask("Quais apagar? [cyan]a[/] = todos 🟢 · ex: [cyan]1,3,5-7[/] · Enter cancela", default="",
                     show_default=False)
    chosen = parse_selection(raw, items)
    if not chosen:
        con.print("[dim]Nada apagado.[/]")
        return

    con.print()
    for i in chosen:
        con.print(f"  [green]✔[/] {i.label}  [bold]{fmt(i.size)}[/]  {i.risk.icon}")
    con.print(f"\n  Total: [bold]{fmt(sum(i.size for i in chosen))}[/]")
    if any(i.risk is LOSS for i in chosen):
        con.print("  [red]Atenção: tem item 🔴 na lista, ele não volta.[/]")
    if not Confirm.ask("\nApagar esses itens?", default=False):
        con.print("[dim]Nada apagado.[/]")
        return

    before = shutil.disk_usage(DATA_VOLUME).free
    for i in chosen:
        if i.guard and (why := i.guard()):
            con.print(f"  [yellow]↷[/] {i.label}: pulei, {why}")
            continue
        with con.status(f"Limpando {i.label}…"):
            i.action()
        con.print(f"  [green]✔[/] {i.label}")

    after = shutil.disk_usage(DATA_VOLUME).free
    msg = Text.assemble(("Liberado: ", "bold"), (fmt(max(0, after - before)), "bold green"),
                        f"  ·  livre agora: {fmt(after)}")
    if any(i.cat == "docker" for i in chosen):
        msg.append("\nO Docker devolve o espaço do Docker.raw aos poucos; se não mudar, reinicie o Docker Desktop.",
                   style="dim")
    con.print(Panel(msg, border_style="green", padding=(0, 2)))


# ---------------------------------------------------------------- alerta

def notify(title: str, body: str, sound: str | None = None) -> None:
    def esc(x: str) -> str:
        return x.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{esc(body)}" with title "{esc(title)}"'
    if sound:
        script += f' sound name "{sound}"'
    sh(["osascript", "-e", script])


def alert_message(free: int) -> tuple[str, str]:
    body = ""
    r = sh(["docker", "system", "df", "--format", "{{json .}}"], timeout=20)
    if r and r.returncode == 0:
        reclaim = sum(parse_size(json.loads(line).get("Reclaimable", "")) for line in r.stdout.splitlines())
        try:
            raw = DOCKER_RAW.stat().st_blocks * 512
            body = f"Docker ocupa {fmt(raw)} (~{fmt(reclaim)} liberáveis). "
        except OSError:
            pass
    return f"💾 Disco quase cheio: {fmt(free)} livres", body + "Rode hogs no terminal."


def check(threshold_gb: float) -> None:
    free = shutil.disk_usage(DATA_VOLUME).free
    if free >= threshold_gb * GB:
        return
    critical = free < 10 * GB
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    if time.time() - state.get("last", 0) < (3600 if critical else 6 * 3600):
        return
    title, body = alert_message(free)
    notify(title, body, "Basso" if critical else None)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last": time.time()}))


def alert(arg: str) -> None:
    uid = os.getuid()
    if arg == "status":
        if not PLIST.exists():
            print("Aviso desligado. Ligue com: hogs --alert 30")
            return
        args = plistlib.loads(PLIST.read_bytes())["ProgramArguments"]
        threshold = args[args.index("--threshold") + 1]
        r = sh(["launchctl", "print", f"gui/{uid}/{LABEL}"])
        state = "ativo" if r and r.returncode == 0 else "instalado mas não carregado (rode hogs --alert de novo)"
        print(f"Aviso {state}: notifica quando o espaço livre cair abaixo de {threshold} GB (checa a cada 1h).")
    elif arg == "off":
        sh(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
        PLIST.unlink(missing_ok=True)
        print("Aviso desligado.")
    elif arg == "test":
        free = shutil.disk_usage(DATA_VOLUME).free
        notify(*alert_message(free))
        print("Notificação de teste enviada. Se não apareceu, libere notificações do "
              "'Editor de Scripts' em Ajustes do Sistema › Notificações.")
    else:
        try:
            threshold = float(arg.replace(",", "."))
        except ValueError:
            sys.exit("Use: hogs --alert <GB> | off | test")
        plist = {
            "Label": LABEL,
            "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
                                 "--check", "--threshold", f"{threshold:g}"],
            "StartInterval": 3600,
            "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            "StandardOutPath": str(LOG),
            "StandardErrorPath": str(LOG),
        }
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        sh(["launchctl", "bootout", f"gui/{uid}/{LABEL}"])
        PLIST.write_bytes(plistlib.dumps(plist))
        r = sh(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
        if not r or r.returncode != 0:
            sys.exit(f"Não consegui ativar: {r.stderr.strip() if r else 'launchctl não encontrado'}")
        print(f"Aviso ativado: notifica quando o espaço livre cair abaixo de {threshold:g} GB (checa a cada 1h).\n"
              f"Desligar: hogs --alert off")


# ---------------------------------------------------------------- main

def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(130)


def run() -> None:
    p = argparse.ArgumentParser(prog="hogs", description="Mostra quem está comendo seu disco e ajuda a limpar.")
    p.add_argument("--version", action="version", version=f"hogs {__version__}")
    p.add_argument("--clean", action="store_true", help="escolhe o que apagar")
    p.add_argument("--alert", nargs="?", const="status", metavar="GB|off|test",
                   help="aviso automático quando o espaço livre cair abaixo de GB")
    p.add_argument("--apps", nargs="?", const=20, type=int, metavar="N",
                   help="só os N apps menos usados (padrão 20)")
    p.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--threshold", type=float, default=30, help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.check:
        return check(args.threshold)
    if args.alert:
        return alert(args.alert)
    if not HAS_RICH:
        sys.exit("hogs precisa da biblioteca rich:  pip install rich")

    con = Console()
    if args.apps:
        with con.status("[bold]🐷 Vendo quais apps você não abre há tempo…[/]", spinner="dots"):
            apps = gather_apps()
        con.print(apps_table(apps, args.apps) or "Nenhum app encontrado.")
        con.print(apps_footer())
        return
    snapshot = gather(con)
    if args.clean:
        clean(con, snapshot)
    else:
        report(con, snapshot)


if __name__ == "__main__":
    main()
