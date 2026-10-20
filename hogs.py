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

__version__ = "0.2.0"

HOME = Path.home()
LIB = HOME / "Library"
APP_SUPPORT = LIB / "Application Support"
CONTAINERS = LIB / "Containers"
GROUP_CONTAINERS = LIB / "Group Containers"
CACHES = LIB / "Caches"
DATA_VOLUME = "/System/Volumes/Data" if os.path.ismount("/System/Volumes/Data") else "/"
DOCKER_CONTAINER = CONTAINERS / "com.docker.docker"
DOCKER_RAW = DOCKER_CONTAINER / "Data/vms/0/data/Docker.raw"

DOCKER_SETTINGS = GROUP_CONTAINERS / "group.com.docker/settings-store.json"
DAEMON_JSON = HOME / ".docker/daemon.json"

LABEL = "dev.hogs.agent"
OLD_LABELS = ["dev.hogs.alert"]  # nome usado na 0.1.0
PLIST = LIB / "LaunchAgents" / f"{LABEL}.plist"
DATA_DIR = APP_SUPPORT / "hogs"
STATE = DATA_DIR / "state.json"
CONFIG = DATA_DIR / "config.json"
HISTORY = DATA_DIR / "history.jsonl"
LOG = LIB / "Logs" / "hogs.log"
HISTORY_DAYS = 90

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
    builders: list[dict] = field(default_factory=list)
    builder_caches: list[dict] = field(default_factory=list)   # volumes de estado do BuildKit
    current_builder: str = ""


def list_builders() -> list[dict]:
    r = sh(["docker", "buildx", "ls", "--format", "json"], timeout=30)
    builders = []
    for line in (r.stdout.splitlines() if r and r.returncode == 0 else []):
        try:
            b = json.loads(line)
        except ValueError:
            continue
        nodes = b.get("Nodes") or []
        builders.append({"name": b.get("Name", ""), "driver": b.get("Driver", ""),
                         "nodes": [n.get("Name", "") for n in nodes],
                         "running": any(n.get("Status") == "running" for n in nodes)})
    return builders


def current_builder() -> str:
    r = sh(["docker", "buildx", "inspect"], timeout=30)
    for line in (r.stdout.splitlines() if r and r.returncode == 0 else []):
        if line.startswith("Name:"):
            return line.split(":", 1)[1].strip()
    return ""


def builder_of_volume(volume: str, builders: list[dict]) -> str | None:
    """Builders docker-container guardam o cache em `buildx_buildkit_<node>_state`."""
    m = re.fullmatch(r"buildx_buildkit_(.+)_state", volume)
    if not m:
        return None
    return next((b["name"] for b in builders if m.group(1) in b["nodes"]), m.group(1))


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

    d.builders = list_builders()
    d.current_builder = current_builder()
    for vol in v.get("Volumes") or []:
        name, size = vol.get("Name", ""), parse_size(vol.get("Size", ""))
        if builder := builder_of_volume(name, d.builders):
            d.builder_caches.append({"builder": builder, "volume": name, "size": size})
            continue
        d.volumes.append({"name": name, "size": size, "used": vol.get("Links", "0") not in ("0", "N/A")})
    return d


def prune_builder(name: str, max_gb: float | None = None) -> subprocess.CompletedProcess | None:
    """Sem limite: apaga todo o cache do builder. Com limite: mantém só os N GB mais recentes."""
    if max_gb is None:
        return sh(["docker", "buildx", "prune", "--builder", name, "-a", "-f"], timeout=900)
    limit = f"{max_gb:g}gb"
    r = sh(["docker", "buildx", "prune", "--builder", name, "-f", "--max-used-space", limit], timeout=900)
    if r and r.returncode != 0 and "unknown flag" in r.stderr:      # buildx < 0.17
        r = sh(["docker", "buildx", "prune", "--builder", name, "-f", "--keep-storage", limit], timeout=900)
    return r


def trim_builders(max_gb: float) -> list[tuple[str, int, str]]:
    """Aplica o limite em cada builder rodando; devolve (builder, liberado, erro).

    Os builders do driver `docker` dividem o cache do próprio Docker e só aceitam comandos pelo
    contexto atual (`default` falha no Docker Desktop, que usa `desktop-linux`), então só esse é podado."""
    ctx = sh(["docker", "context", "show"])
    context = ctx.stdout.strip() if ctx and ctx.returncode == 0 else "default"
    done = []
    for b in list_builders():
        if not b["running"] or (b["driver"] == "docker" and b["name"] != context):
            continue
        r = prune_builder(b["name"], max_gb)
        if not r or r.returncode != 0:
            done.append((b["name"], 0, (r.stderr.strip().splitlines() or ["erro"])[-1] if r else "docker não respondeu"))
            continue
        freed = 0
        for line in r.stdout.splitlines():
            if line.startswith("Total:"):
                freed = parse_size(line.split(":", 1)[1])
        done.append((b["name"], freed, ""))
    return done


def docker_clean_items(d: Docker) -> list[Item]:
    if not d.running:
        return []
    items: list[Item] = []
    if d.build_cache:
        items.append(Item("docker", "Docker: cache de build", d.build_cache, SAFE,
                          lambda: sh(["docker", "builder", "prune", "-a", "-f"], timeout=900),
                          "próximo build fica mais lento"))

    for bc in d.builder_caches:
        if bc["size"] >= 1e6:
            items.append(Item("docker", f"Docker: cache do builder {bc['builder']}", bc["size"], SAFE,
                              lambda n=bc["builder"]: prune_builder(n),
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
    # guarda a partir de 100 MB pro histórico; o relatório só mostra os de 1 GB+
    others = sorted(((short(p), sizes[p]) for p in candidates if sizes.get(p, 0) >= 100e6), key=lambda x: -x[1])
    usage = shutil.disk_usage(DATA_VOLUME)
    return Snapshot(usage.total, usage.free, docker, items, others, apps)


# ---------------------------------------------------------------- histórico
#
# Duas fontes gravam em history.jsonl: o agente (de hora em hora, só espaço livre e Docker.raw,
# porque precisa ser instantâneo) e cada relatório (tudo, com "full": true).

def history_load(days: float = HISTORY_DAYS) -> list[dict]:
    cutoff = time.time() - days * 86400
    try:
        lines = HISTORY.read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("t", 0) >= cutoff:
            records.append(rec)
    return sorted(records, key=lambda r: r["t"])


def history_append(rec: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        with HISTORY.open() as f:
            first = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        first = {}
    if first.get("t", time.time()) < time.time() - HISTORY_DAYS * 86400:
        HISTORY.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in history_load()))


def light_record() -> dict:
    rec = {"t": time.time(), "free": shutil.disk_usage(DATA_VOLUME).free}
    try:
        rec["docker_raw"] = DOCKER_RAW.stat().st_blocks * 512
    except OSError:
        pass
    return rec


def snapshot_sizes(s: Snapshot) -> dict[str, int]:
    d = s.docker
    sizes = {"Docker.raw": d.raw_real}
    if d.running:
        sizes["Docker: imagens"] = sum(i["size"] for i in d.images)
        sizes["Docker: cache de build"] = d.build_cache + sum(b["size"] for b in d.builder_caches)
        sizes["Docker: volumes"] = sum(v["size"] for v in d.volumes)
    sizes.update({i.label: i.size for i in s.items})
    sizes.update(dict(s.others))
    return {k: v for k, v in sizes.items() if v >= 10e6}


def record_snapshot(s: Snapshot) -> None:
    history_append({"t": time.time(), "free": s.free, "docker_raw": s.docker.raw_real,
                    "full": True, "sizes": snapshot_sizes(s)})


def value_at(records: list[dict], key: str, t: float) -> tuple[float, int] | None:
    """Último registro com `key` até o instante t; se não houver, o mais antigo."""
    found = [r for r in records if key in r]
    if not found:
        return None
    before = [r for r in found if r["t"] <= t]
    r = before[-1] if before else found[0]
    return r["t"], r[key]


def baseline(records: list[dict], now: float) -> dict | None:
    """Relatório completo pra comparar: o mais recente com pelo menos 20h; senão o mais antigo com 1h+."""
    full = [r for r in records if r.get("full")]
    day_old = [r for r in full if r["t"] <= now - 20 * 3600]
    if day_old:
        return day_old[-1]
    hour_old = [r for r in full if r["t"] <= now - 3600]
    return hour_old[0] if hour_old else None


def growth(current: dict[str, int], base: dict[str, int], min_delta: float = 100e6) -> list[tuple[str, int]]:
    deltas = [(k, v - base.get(k, 0)) for k, v in current.items()]
    return sorted((d for d in deltas if d[1] >= min_delta), key=lambda d: -d[1])


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(records: list[dict], key: str, now: float, days: int = 7, buckets: int = 28) -> str:
    start, width = now - days * 86400, days * 86400 / buckets
    vals: list[int | None] = [None] * buckets
    for r in records:
        if key in r and r["t"] >= start:
            vals[min(buckets - 1, int((r["t"] - start) / width))] = r[key]
    known = [v for v in vals if v is not None]
    if len(known) < 2:
        return ""
    lo, hi = min(known), max(known)
    chars = [" " if v is None else SPARK[round((v - lo) / (hi - lo) * (len(SPARK) - 1)) if hi > lo else 0]
             for v in vals]
    return "".join(chars).lstrip()


def fmt_delta(n: float) -> str:
    return ("+" if n >= 0 else "−") + fmt(abs(n))


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
        *[(f"Cache do builder {bc['builder']}", bc["size"], SAFE, "volume do BuildKit; o daemon.json não limita")
          for bc in d.builder_caches],
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


def trend(s: Snapshot, records: list[dict], now: float) -> Text | None:
    """'últimas 24h: livre −3 GB · Docker +17 GB' e as minicurvas da semana."""
    past = value_at(records, "free", now - 86400)
    if not past or now - past[0] < 3600:
        return None
    when = "últimas 24h" if now - past[0] >= 20 * 3600 else f"desde {ago(datetime.fromtimestamp(past[0], timezone.utc))}"
    free_delta = s.free - past[1]
    t = Text.assemble(("📈 ", ""), (f"{when}: ", "bold"), "livre ",
                      (fmt_delta(free_delta), "red" if free_delta < -GB else "green" if free_delta > GB else ""))
    raw_past = value_at(records, "docker_raw", now - 86400)
    if raw_past and s.docker.raw_real:
        raw_delta = s.docker.raw_real - raw_past[1]
        t.append("  ·  Docker ").append(fmt_delta(raw_delta), style="red" if raw_delta > GB else "")
    points = records + [{"t": now, "free": s.free, "docker_raw": s.docker.raw_real}]
    curves = [(label, sparkline(points, key, now, buckets=21)) for label, key in (("livre", "free"), ("Docker", "docker_raw"))]
    if any(c for _, c in curves):
        t.append("\n   7 dias   ", style="dim")
        for label, curve in curves:
            if curve:
                t.append(f"{label} ", style="dim").append(curve, style="cyan").append("   ")
    return t


def header(s: Snapshot, records: list[dict] | None = None) -> Panel:
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
    body = [line, Text(), bar, legend]
    if records and (t := trend(s, records, time.time())) is not None:
        body += [Text(), t]
    return Panel(Group(*body), title="[bold]🐷 hogs[/]  [dim]quem está comendo seu disco[/]",
                 title_align="left", border_style="grey50", padding=(1, 2))


def growth_table(s: Snapshot, records: list[dict]) -> Table | Text:
    now = time.time()
    base = baseline(records, now)
    if base is None:
        return Text("📈 O histórico começou agora: a partir de amanhã o hogs mostra o que mais cresceu. "
                    "Com o agente ligado (hogs --alert ou --docker-gc), ele também registra de hora em hora.",
                    style="dim")
    rows = growth(snapshot_sizes(s), base.get("sizes", {}))[:8]
    since = ago(datetime.fromtimestamp(base["t"], timezone.utc))
    if not rows:
        return Text(f"📈 Nada cresceu mais de 100 MB desde o último relatório ({since}).", style="dim")
    top = rows[0][1]
    t = Table(title=f"📈 O que mais cresceu ({since})", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    t.add_column("Item", ratio=3, no_wrap=True, overflow="ellipsis")
    t.add_column("Cresceu", justify="right", no_wrap=True)
    t.add_column("", no_wrap=True, width=14)
    t.add_column("Agora", justify="right", style="dim", no_wrap=True)
    current = snapshot_sizes(s)
    for label, delta in rows:
        color = "red" if delta >= GB else "yellow"
        t.add_row(label, Text(fmt_delta(delta), style=f"bold {color}"), mini_bar(delta, top, color), fmt(current[label]))
    return t


def prevention_panel(s: Snapshot) -> Panel | None:
    d = s.docker
    if not d.running and not d.raw_real:
        return None
    cfg = load_config()
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column()

    def row(ok: bool | None, text: str, hint: str = "") -> None:
        icon = Text("✔", style="green") if ok else Text("ℹ", style="blue") if ok is None else Text("!", style="bold yellow")
        grid.add_row(icon, Text.assemble(text, (f"  {hint}" if hint else "", "dim")))

    gc_gb = cfg.get("builder_cache_gb")
    if gc_gb:
        row(True, f"Limite do cache de build: no máximo {gc_gb:g} GB por builder", "o hogs confere a cada 1h")
    else:
        row(False, "Limite do cache de build: desligado", "ligue com: hogs --docker-gc 20")

    daemon_gc = read_json(DAEMON_JSON).get("builder", {}).get("gc", {})
    builder = next((b for b in d.builders if b["name"] == d.current_builder), None)
    if daemon_gc.get("enabled"):
        text = f"Limpeza automática do Docker (daemon.json): mantém {daemon_gc.get('defaultKeepStorage', '?')}"
        if builder and builder["driver"] != "docker":
            ignores = f"o builder ativo ({builder['name']}) roda num container e ignora essa regra"
            row(bool(gc_gb), text, f"{ignores}; o --docker-gc cobre ele" if gc_gb else f"mas {ignores}")
        else:
            row(True, text)
    else:
        row(bool(gc_gb), "Limpeza automática do Docker (daemon.json): desligada",
            "o --docker-gc cobre isso" if gc_gb else "Docker › Settings › Docker Engine")

    if read_json(DOCKER_SETTINGS).get("DiskTRIM") is False:
        row(False, "TRIM desligado: o espaço apagado no Docker não volta pro macOS",
            "Docker › Settings › Resources › Advanced")
    if d.raw_limit:
        row(None, f"Teto do disco do Docker: {fmt(d.raw_limit)} ({d.raw_limit / s.total:.0%} do seu disco)",
            "é só um limite; reduzir apaga todas as imagens, containers e volumes")
    return Panel(grid, title="[bold]🛡  Prevenção[/]", title_align="left", border_style="grey50", padding=(0, 2))


def report(con: Console, s: Snapshot, records: list[dict] | None = None) -> None:
    records = records or []
    con.print(header(s, records))
    con.print(growth_table(s, records))
    con.print()
    for t in (category_table("🐳 Docker", docker_rows(s.docker)), prevention_panel(s), unused_images_table(s.docker),
              category_table("🧰 Caches de dev", [(i.label, i.size, i.risk, i.hint) for i in s.items if i.cat == "dev"]),
              category_table("📦 Apps e dados", [(i.label, i.size, i.risk, i.hint) for i in s.items if i.cat == "apps"]),
              category_table("🔎 Outros grandes (fora da lista acima)",
                             [(label, size, INFO, "") for label, size in s.others if size >= GB][:12])):
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


# ---------------------------------------------------------------- configuração e agente

def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict:
    return read_json(CONFIG)


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def ensure_agent(cfg: dict) -> str | None:
    """Instala o agente do launchd se algum recurso estiver ligado; senão remove. Devolve o erro, se houver."""
    uid = os.getuid()
    for label in [LABEL, *OLD_LABELS]:
        sh(["launchctl", "bootout", f"gui/{uid}/{label}"])
    for label in OLD_LABELS:
        (PLIST.parent / f"{label}.plist").unlink(missing_ok=True)
    if not (cfg.get("alert_gb") or cfg.get("builder_cache_gb")):
        PLIST.unlink(missing_ok=True)
        return None
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--agent"],
        "StartInterval": 3600,
        "RunAtLoad": True,
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    r = sh(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)])
    if not r or r.returncode != 0:
        return r.stderr.strip() if r else "launchctl não encontrado"
    return None


def agent_run() -> None:
    """Roda de hora em hora pelo launchd: grava o histórico, poda o cache de build e avisa se o disco encher."""
    cfg = load_config()
    history_append(light_record())
    if cfg.get("builder_cache_gb"):
        for name, freed, err in trim_builders(cfg["builder_cache_gb"]):
            if err:
                log(f"builder {name}: falhou: {err}")
            elif freed:
                log(f"builder {name}: liberou {fmt(freed)}")
    if cfg.get("alert_gb"):
        check(cfg["alert_gb"])


def agent_status() -> None:
    cfg = load_config()
    r = sh(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    loaded = bool(r and r.returncode == 0)
    if cfg.get("alert_gb") or cfg.get("builder_cache_gb"):
        print("Agente " + ("ativo, roda a cada 1h." if loaded else "configurado mas não carregado: rode o comando de novo."))
    else:
        print("Agente desligado.")
    alert_gb, gc_gb = cfg.get("alert_gb"), cfg.get("builder_cache_gb")
    print(f"  Aviso de disco cheio:     {f'abaixo de {alert_gb:g} GB livres' if alert_gb else 'desligado (hogs --alert 30)'}")
    print(f"  Limite do cache de build: {f'{gc_gb:g} GB por builder' if gc_gb else 'desligado (hogs --docker-gc 20)'}")
    records = history_load()
    if records:
        print(f"  Histórico: {len(records)} registros desde {datetime.fromtimestamp(records[0]['t']):%d/%m/%Y %H:%M}")
    else:
        print("  Histórico: vazio")


def parse_gb(arg: str, usage: str) -> float:
    try:
        value = float(arg.replace(",", "."))
    except ValueError:
        sys.exit(usage)
    if value <= 0:
        sys.exit(usage)
    return value


def set_feature(key: str, value: float | None) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
    if err := ensure_agent(cfg):
        sys.exit(f"Não consegui ativar o agente: {err}")


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
        except OSError:
            raw = 0
        if raw:
            body = f"Docker ocupa {fmt(raw)} (~{fmt(reclaim)} liberáveis). "
            past = value_at(history_load(2), "docker_raw", time.time() - 86400)
            if past and raw - past[1] > 2 * GB:
                body += f"Cresceu {fmt(raw - past[1])} em 24h. "
    return f"💾 Disco quase cheio: {fmt(free)} livres", body + "Rode hogs no terminal."


def check(threshold_gb: float) -> None:
    free = shutil.disk_usage(DATA_VOLUME).free
    if free >= threshold_gb * GB:
        return
    critical = free < 10 * GB
    state = read_json(STATE)
    if time.time() - state.get("last", 0) < (3600 if critical else 6 * 3600):
        return
    title, body = alert_message(free)
    notify(title, body, "Basso" if critical else None)
    log(f"aviso enviado: {title}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last": time.time()}))


def alert(arg: str) -> None:
    usage = "Use: hogs --alert <GB> | off | test"
    if arg == "status":
        agent_status()
    elif arg == "off":
        set_feature("alert_gb", None)
        print("Aviso de disco desligado.")
    elif arg == "test":
        notify(*alert_message(shutil.disk_usage(DATA_VOLUME).free))
        print("Notificação de teste enviada. Se não apareceu, libere notificações do "
              "'Editor de Scripts' em Ajustes do Sistema › Notificações.")
    else:
        gb = parse_gb(arg, usage)
        set_feature("alert_gb", gb)
        print(f"Aviso ativado: notifica quando o espaço livre cair abaixo de {gb:g} GB (checa a cada 1h).\n"
              "Desligar: hogs --alert off")


def docker_gc(arg: str) -> None:
    usage = "Use: hogs --docker-gc <GB> | off"
    if arg == "status":
        agent_status()
        return
    if arg == "off":
        set_feature("builder_cache_gb", None)
        print("Limite do cache de build desligado.")
        return
    gb = parse_gb(arg, usage)
    print(f"Aplicando agora: cada builder fica com no máximo {gb:g} GB de cache…")
    done = trim_builders(gb)
    if not done:
        print("  Docker não está rodando; o limite vale a partir da próxima checagem.")
    for name, freed, err in done:
        status = f"falhou: {err}" if err else f"liberou {fmt(freed)}" if freed else "já estava dentro do limite"
        print(f"  {name}: {status}")
    set_feature("builder_cache_gb", gb)
    print(f"Limite ativado: o hogs confere a cada 1h e apaga o cache mais antigo acima de {gb:g} GB.\n"
          "Desligar: hogs --docker-gc off")


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
    p.add_argument("--docker-gc", nargs="?", const="status", metavar="GB|off",
                   help="mantém o cache de build de cada builder abaixo de GB (confere a cada 1h)")
    p.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.agent:
        return agent_run()
    if args.alert:
        return alert(args.alert)
    if args.docker_gc:
        return docker_gc(args.docker_gc)
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
    records = history_load()
    record_snapshot(snapshot)
    if args.clean:
        clean(con, snapshot)
    else:
        report(con, snapshot, records)


if __name__ == "__main__":
    main()
