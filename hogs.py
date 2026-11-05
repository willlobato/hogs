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
import math
import os
import plistlib
import re
import select
import shlex
import shutil
import stat
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, TextIO

try:
    from rich import box
    from rich.cells import cell_len
    from rich.columns import Columns
    from rich.console import Console, Group, RenderableType
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

__version__ = "0.5.0"

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
BIG_ITEM = 500e6         # a partir daqui vale uma linha própria: cache de app, arquivo parado, imagem de registry
OLD_CONTAINER_DAYS = 30

# Ajustes do config.json (ver setting()); cada padrão vale para todo o código, inclusive parâmetros com padrão.
DEFAULTS: dict[str, object] = {
    "rollback_tags": ["antes", "backup", "bkp", "before", "rollback"],
    "rollback_days": 2,
    "stale_worktree_days": 30,
}


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
EVAL = Risk("🟡", "para avaliar", "yellow", 1)


@dataclass
class Item:
    cat: str                     # docker | dev | apps | worktree
    label: str
    size: int = 0
    risk: Risk = INFO
    action: Callable[[], object] | None = None
    hint: str = ""
    guard: Callable[[], str | None] | None = None
    kind: str = ""               # "pessoal" | "ponto_de_volta": separa os 🟡 para avaliar no Recomendado
    path: Path | None = None     # onde o item mora no disco (para abrir no Finder e mostrar no 🔎)
    worktree: Worktree | None = None   # o worktree, quando o item é um só


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
    # Os limites são onde o arredondamento vira 1000 da unidade menor (999,7 MB → "1,0 GB", não "1000 MB").
    if n >= 999.5e6:
        s = f"{n / GB:.1f} GB"
    elif n >= 999.5e3:
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


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def preview(names: list[str], n: int = 3) -> str:
    return ", ".join(names[:n]) + ("…" if len(names) > n else "")


def require(r: subprocess.CompletedProcess | None, what: str) -> subprocess.CompletedProcess:
    """Comando ausente, sem resposta ou com código ≠ 0 vira RuntimeError com a última linha do erro."""
    if r is None:
        raise RuntimeError(f"{what}: não encontrado ou sem resposta")
    if r.returncode:
        raise RuntimeError((r.stderr.strip().splitlines() or [f"{what} falhou"])[-1])
    return r


def run_all(cmds: list[list[str]], timeout: int = 60) -> None:
    """Roda todos os comandos mesmo se algum falhar; no fim, falha se algum falhou."""
    errors = []
    for cmd in cmds:
        try:
            require(sh(cmd, timeout=timeout), cmd[0])
        except RuntimeError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError((f"{len(errors)} de {len(cmds)} falharam: " if len(cmds) > 1 else "") + errors[0])


MAXCOMLEN = 16   # o kernel guarda o nome do processo cortado nesse tamanho


def ere_escape(text: str) -> str:
    """Texto literal numa regex estendida (a do pgrep): "App (Beta)" não vira grupo."""
    return re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", text)


def process_running(name: str) -> bool:
    """Processo com esse executável aberto: pelo nome inteiro, pelo nome cortado em MAXCOMLEN ou pelo caminho
    do executável do app. Qualquer um que casar conta como aberto (na dúvida, aberto)."""
    patterns = [["-x", ere_escape(name)]]
    if len(name) > MAXCOMLEN:
        patterns.append(["-x", ere_escape(name[:MAXCOMLEN])])
    patterns.append(["-f", ere_escape(f"/Contents/MacOS/{name}") + "( |$)"])
    for args in patterns:
        r = sh(["pgrep", *args])
        if r and r.returncode == 0:
            return True
    return False


def plist_dict(data: bytes) -> dict | None:
    """Plist (XML ou binário) que é um dicionário; qualquer outra coisa ou erro de leitura vira None."""
    try:
        d = plistlib.loads(data)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def plist_file(path: Path) -> dict:
    try:
        return plist_dict(path.read_bytes()) or {}
    except OSError:
        return {}


def plist_cmd(cmd: list[str]) -> dict | None:
    """Saída -plist de um comando; None se o comando falhou ou a saída não é um dicionário."""
    r = sh(cmd)
    if not r or r.returncode != 0 or not r.stdout:
        return None
    return plist_dict(r.stdout.encode())


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


def _age_days(ts: str) -> int | None:
    """Dias desde o instante do Docker; None se vazio, ilegível ou zerado (0001-01-01: nunca aconteceu)."""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if dt.year < 2000:
        return None
    return (datetime.now(timezone.utc) - dt).days


# Estados em que o container está vivo: não é "parado" e o projeto compose dele conta como rodando.
ACTIVE_STATES = {"running", "paused", "restarting", "removing"}


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
    mounts: list[Path] = field(default_factory=list)   # origens de bind mount de qualquer container
    mounts_ok: bool = False                            # True só se o inspect respondeu
    live: list[str] = field(default_factory=list)      # containers rodando (nomes)
    released: dict[str, str] = field(default_factory=dict)  # imagem atual → branch padrão (de um clone local) com o commit dela


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


def bind_volume_folders(names: list[str]) -> list[Path]:
    """Pastas do host por trás de volumes nomeados com bind (`--opt type=none --opt o=bind --opt device=/pasta`,
    comum em compose de banco). Qualquer falha levanta ValueError: na dúvida, mounts desconhecidos."""
    if not names:
        return []
    r = sh(["docker", "volume", "inspect", "--format", "{{json .Options}}", *names])
    if not r or r.returncode != 0:
        raise ValueError("docker volume inspect falhou")
    lines = [line for line in r.stdout.splitlines() if line.strip()]
    if len(lines) != len(names):
        raise ValueError("docker volume inspect respondeu pela metade")
    folders = []
    for line in lines:
        opts = json.loads(line) or {}
        if "bind" in str(opts.get("o", "")) and opts.get("device"):
            folders.append(Path(opts["device"]).resolve())
    return folders


def image_labels(ids: list[str]) -> dict[str, dict[str, str]]:
    """Etiquetas de cada imagem pelo ID curto, numa só chamada de leitura; qualquer falha devolve {}."""
    if not ids:
        return {}
    r = sh(["docker", "image", "inspect", "--format", "{{.Id}}\t{{json .Config.Labels}}"] + sorted(set(ids)))
    out: dict[str, dict[str, str]] = {}
    if not r or r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        image_id, _, raw = line.partition("\t")
        try:
            found = json.loads(raw)
        except ValueError:
            continue
        if isinstance(found, dict):
            out[image_id.removeprefix("sha256:")[:12]] = {
                k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}
    return out


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
        image_id, digest = img.get("ID", "").removeprefix("sha256:")[:12], img.get("Digest", "")
        # Sem tag, `docker rmi repo` seria repo:latest, outra imagem: remove pelo digest ou pelo ID.
        if tag != "<none>":
            name = ref = f"{repo}:{tag}"
        elif repo != "<none>" and digest.startswith("sha256:"):
            name, ref = f"{repo}@{digest[:19]}…", f"{repo}@{digest}"
        else:
            name, ref = repo, image_id
        d.images.append({
            "repo": repo, "tag": tag, "id": image_id, "name": name, "ref": ref,
            "size": parse_size(img.get("UniqueSize", "")), "used": used,
            "origin": image_origin(repo), "created": img.get("CreatedSince", ""),
            "created_at": parse_epoch(img.get("CreatedAt", "")),
        })
    labels = image_labels([i["id"] for i in d.images if i["tag"] != "<none>" and i["id"]])
    for i in d.images:
        i["labels"] = labels.get(i["id"], {})
    # Com o containerd, a soma dos UniqueSize passa do total real (camadas contadas em dobro).
    # Escala cada grupo pra bater com os totais do `docker system df`.
    for used, target in ((False, images_reclaimable), (True, images_total - images_reclaimable)):
        group = [i for i in d.images if i["used"] == used]
        reported = sum(i["size"] for i in group)
        if reported > target > 0:
            for i in group:
                i["size"] = int(i["size"] * target / reported)

    containers = v.get("Containers") or []
    if not containers:
        d.mounts_ok = True
    else:
        r = sh(["docker", "inspect", "--format", "{{json .Mounts}}"] + [c["ID"] for c in containers])
        try:
            if not r or r.returncode != 0:
                raise ValueError("inspect falhou")
            found, volumes = [], []
            for line in r.stdout.splitlines():
                if line.strip():
                    for m in json.loads(line) or []:
                        if m.get("Type") == "bind" and m.get("Source"):
                            found.append(Path(m["Source"]).resolve())
                        elif m.get("Type") == "volume" and m.get("Name") and m["Name"] not in volumes:
                            volumes.append(m["Name"])
            found += bind_volume_folders(volumes)
            d.mounts, d.mounts_ok = sorted(set(found)), True
        except (ValueError, TypeError, AttributeError, KeyError, OSError, RuntimeError):
            d.mounts, d.mounts_ok = [], False
    running_projects = {
        _labels(c.get("Labels", "")).get("com.docker.compose.project")
        for c in containers if c.get("State") in ACTIVE_STATES
    } - {None}
    d.live = sorted(c.get("Names", c["ID"][:12]) for c in containers if c.get("State") in ACTIVE_STATES)
    stopped = [c for c in containers if c.get("State") not in ACTIVE_STATES]
    ages: dict[str, int | None] = {}
    if stopped:
        r = sh(["docker", "inspect", "--format", "{{.Id}}|{{.State.FinishedAt}}|{{.Created}}"]
               + [c["ID"] for c in stopped])
        for line in (r.stdout.splitlines() if r else []):
            cid, finished_at, created = (line.split("|") + ["", ""])[:3]
            # Nunca iniciado: FinishedAt zerado, vale a criação. Idade desconhecida nunca conta como antiga.
            age = _age_days(finished_at)
            ages[cid] = age if age is not None else _age_days(created)
    for c in stopped:
        project = _labels(c.get("Labels", "")).get("com.docker.compose.project")
        d.stopped.append({
            "name": c.get("Names", c["ID"][:12]), "image": c.get("Image", ""),
            "size": parse_size(c.get("Size", "")), "age": ages.get(c["ID"]),
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


MOUNTS_FILE = DATA_DIR / "mounts.json"


def known_mounts(d: Docker) -> tuple[list[Path], bool]:
    """Pastas montadas por containers e se a lista é confiável. Sem resposta do Docker, usa a última salva."""
    if d.mounts_ok:
        try:
            MOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            MOUNTS_FILE.write_text(json.dumps({"mounts": [str(m) for m in d.mounts]}))
        except OSError:
            pass
        return d.mounts, True
    saved = read_json(MOUNTS_FILE).get("mounts")
    if isinstance(saved, list) and all(isinstance(m, str) and m for m in saved):
        return [Path(m) for m in saved], True
    return [], not shutil.which("docker")


def under_mount(path: Path, mounts: list[Path], containing: bool = True) -> bool:
    """`path` dentro de uma pasta montada (ou igual a ela); com `containing`, também se contém uma."""
    try:
        path = path.resolve()
    except (OSError, RuntimeError):
        return True   # na dúvida, protege
    for m in mounts:
        try:
            m = m.resolve()
        except (OSError, RuntimeError):
            pass
        if path == m or m in path.parents or (containing and path in m.parents):
            return True
    return False


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


def parse_epoch(text: str) -> float | None:
    """Data com fuso em epoch: `CreatedAt` do docker ("2026-11-04 20:04:12 -0300 -03") ou do `mdls`
    ("2025-01-02 03:04:05 +0000"); o que vem depois do fuso é ignorado e o ilegível vira None."""
    try:
        return datetime.strptime(" ".join(text.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def rollback_base(tag: str, words: list[str]) -> str | None:
    """Tag de ponto de volta: "<w>-..." volta para "latest"; "<base>-<w>-..." volta para "<base>"."""
    parts = tag.lower().split("-")
    ws = {w.lower() for w in words}
    if len(parts) > 1 and parts[0] in ws:
        return "latest"
    for i in range(1, len(parts) - 1):
        if parts[i] in ws:
            return tag[:len("-".join(parts[:i]))]
    return None


def tag_date(tag: str) -> float | None:
    """Data embutida no nome da tag (AAAAMMDD-HHMM ou AAAAMMDD), em hora local."""
    m = re.search(r"(?<!\d)(\d{8})(?:-(\d{4}))?(?!\d)", tag)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + (m.group(2) or "0000"), "%Y%m%d%H%M")
        return time.mktime(dt.timetuple())
    except (ValueError, OverflowError):
        return None


SEEN_TAGS = DATA_DIR / "seen_tags.json"


def first_seen(refs: list[str], now: float) -> dict[str, float]:
    """Quando o hogs viu cada tag pela primeira vez (o Docker não guarda a data da tag)."""
    old = read_json(SEEN_TAGS)
    seen: dict[str, float] = {}
    for ref in refs:
        t = old.get(ref)
        ok = isinstance(t, (int, float)) and not isinstance(t, bool) and t <= now
        seen[ref] = float(t) if ok else now
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SEEN_TAGS.write_text(json.dumps(seen, indent=2))
    except OSError:
        pass
    return seen


def rollback_age_days(img: dict, current: dict, seen: float, now: float) -> float:
    """Idade pelo sinal mais recente: data no nome da tag, criação da imagem atual ou primeira vez vista."""
    signals = [t for t in (tag_date(img["tag"]), current.get("created_at"), seen) if t is not None]
    return (now - max(signals)) / 86400


def is_old_stopped(c: dict) -> bool:
    """Parado há +30 dias, idade conhecida e sem projeto rodando: vai para o grupo 🟢."""
    return c["age"] is not None and c["age"] >= OLD_CONTAINER_DAYS and not c["project_running"]


def stopped_risk(c: dict) -> Risk:
    return SAFE if is_old_stopped(c) else REDOWN if c["project"] else LOSS


def image_ref(img: dict) -> str:
    return img.get("ref") or img["name"]


OCI_REVISION = "org.opencontainers.image.revision"
OCI_SOURCE = "org.opencontainers.image.source"


def normalize_remote(url: str) -> str:
    """'git@host:Org/Repo.git', 'https://host/org/repo/' e 'ssh://git@host:22/org/repo.git' -> 'host/org/repo'."""
    u = url.strip()
    if "://" in u:
        host, _, rest = u.split("://", 1)[1].partition("/")
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    else:   # forma scp: [user@]host:org/repo
        host, _, rest = u.rsplit("@", 1)[-1].partition(":")
    rest = rest.strip("/")
    if rest.endswith(".git"):
        rest = rest[:-4]
    return f"{host}/{rest}".lower().rstrip("/")


def repo_remotes(path: Path) -> dict[str, str]:
    """Nome de cada remoto → endereço normalizado."""
    r = git_at(path, "config", "--get-regexp", r"^remote\..*\.url$")
    if r is None or r.returncode != 0:
        return {}
    out = {}
    for line in r.stdout.splitlines():
        key, _, url = line.partition(" ")
        m = re.fullmatch(r"remote\.(.+)\.url", key)
        if m and url.strip():
            out[m.group(1)] = normalize_remote(url)
    return out


def commit_sha(path: Path, rev: str) -> str:
    """SHA do commit que `rev` (hexadecimal) abrevia. '' se `rev` não é o começo do SHA de um commit, como uma tag
    ou branch local com nome hexadecimal, que o Git resolveria no lugar do commit."""
    r = git_at(path, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}")
    sha = r.stdout.strip() if r is not None and r.returncode == 0 else ""
    return sha if sha.startswith(rev) else ""


def oci_candidates(d: Docker) -> dict[str, tuple[str, str]]:
    """Imagens atuais de pontos de volta com as duas etiquetas OCI: nome → (revisão, origem normalizada)."""
    candidates: dict[str, tuple[str, str]] = {}
    for _, current in rollback_pairs(d):
        labels = current.get("labels") or {}
        rev, source = labels.get(OCI_REVISION, ""), labels.get(OCI_SOURCE, "")
        if source and re.fullmatch(r"[0-9a-f]{7,64}", rev):
            candidates[current["name"]] = (rev, normalize_remote(source))
    return candidates


def released_images(d: Docker, repos: list[Path]) -> dict[str, str]:
    """Imagens atuais cujo commit (etiqueta OCI) já está na branch padrão de um clone local, a do remoto que bate
    com a etiqueta de origem: nome da imagem → nome curto da branch. Só leitura e sem rede; qualquer dúvida deixa
    a imagem de fora."""
    candidates = oci_candidates(d)
    if not candidates or not repos:
        return {}
    remotes = {repo: repo_remotes(repo) for repo in repos}
    out: dict[str, str] = {}
    for name, (rev, source) in candidates.items():
        for repo, urls in remotes.items():
            for remote in sorted(r for r, url in urls.items() if url == source):
                branch = default_branch(repo, remote)
                sha = commit_sha(repo, rev) if branch else ""
                r = git_at(repo, "merge-base", "--is-ancestor", sha, branch) if sha else None
                if r is not None and r.returncode == 0:
                    out[name] = branch.removeprefix(f"refs/remotes/{remote}/")
                    break
            if name in out:
                break
    return out


def rollback_pairs(d: Docker) -> list[tuple[dict, dict]]:
    """(ponto de volta, imagem atual da mesma origem); sem a imagem atual, não é ponto de volta."""
    words = setting("rollback_tags")
    by_ref = {i["name"]: i for i in d.images}
    pairs: list[tuple[dict, dict]] = []
    for img in d.images:
        if img["used"] or img["origin"] == "none" or img.get("tag", "<none>") == "<none>":
            continue
        base = rollback_base(img["tag"], words)
        current = by_ref.get(f"{img['repo']}:{base}") if base else None
        if current is not None and current is not img:
            pairs.append((img, current))
    return pairs


class Rollback(NamedTuple):
    img: dict
    size: int       # quanto libera
    risk: Risk
    hint: str


RECENT_ROLLBACK = "o QA pode estar em andamento"


def rollback_info(d: Docker) -> dict[int, Rollback]:
    """Cada ponto de volta, pelo id() da imagem em `d.images`, do maior para o menor.
    O relatório e o --clean usam esta mesma função, então o risco mostrado é sempre o mesmo."""
    pairs = rollback_pairs(d)
    if not pairs:
        return {}
    now = time.time()
    seen = first_seen([i["name"] for i, _ in pairs], now)
    days = setting("rollback_days")
    out: dict[int, Rollback] = {}
    for img, current in sorted(pairs, key=lambda p: -p[0]["size"]):
        age = rollback_age_days(img, current, seen[img["name"]], now)
        old = age > days
        hint = f"ponto de volta há {int(age)} dias" if old else f"ponto de volta recente: {RECENT_ROLLBACK}"
        same = bool(img["id"]) and img["id"] == current["id"]
        if same:  # mesma imagem da atual: `rmi` só tira a tag e não libera nada
            hint = "mesma imagem da atual: só remove a tag"
        risk = SAFE if old else EVAL
        branch = d.released.get(current["name"])
        if branch:   # o commit da imagem atual já está na branch padrão: voltar não perde nada
            risk, hint = SAFE, f"o commit da {current['name']} já está na {branch}"
            if same:
                hint = "mesma imagem da atual: só remove a tag"
        out[id(img)] = Rollback(img, 0 if same else img["size"], risk, hint)
    return out


def docker_clean_items(d: Docker) -> list[Item]:
    if not d.running:
        return []
    items: list[Item] = []
    if d.build_cache:
        items.append(Item("docker", "Docker: cache de build", d.build_cache, SAFE,
                          lambda: run_all([["docker", "builder", "prune", "-a", "-f"]], timeout=900),
                          "próximo build fica mais lento"))

    for bc in d.builder_caches:
        if bc["size"] >= 1e6:
            items.append(Item("docker", f"Docker: cache do builder {bc['builder']}", bc["size"], SAFE,
                              lambda n=bc["builder"]: require(prune_builder(n), "docker buildx prune"),
                              "próximo build fica mais lento"))

    untagged = [i for i in d.images if not i["used"] and i["origin"] == "none"]
    if untagged:
        ids = [i["id"] for i in untagged]
        items.append(Item("docker", f"Docker: imagens sem tag ({len(untagged)})", sum(i["size"] for i in untagged),
                          SAFE, lambda ids=ids: run_all([["docker", "rmi", x] for x in ids]), "versões antigas órfãs"))

    old = [c for c in d.stopped if is_old_stopped(c)]
    if old:
        names = [c["name"] for c in old]
        items.append(Item("docker", f"Docker: containers parados há +{OLD_CONTAINER_DAYS} dias ({len(old)})",
                          sum(c["size"] for c in old), SAFE,
                          lambda names=names: run_all([["docker", "rm", n] for n in names]), preview(names)))
    for c in d.stopped:
        if is_old_stopped(c):
            continue
        why = (f"projeto {c['project']} está rodando" if c["project_running"]
               else f"parado há {c['age']} dias" if c["age"] is not None else "idade desconhecida")
        items.append(Item("docker", f"Docker: container {c['name']}", c["size"], stopped_risk(c),
                          lambda n=c["name"]: run_all([["docker", "rm", n]]), why))

    rollbacks = rollback_info(d)
    for rb in rollbacks.values():
        items.append(Item("docker", f"Ponto de volta {rb.img['name']}", rb.size, rb.risk,
                          lambda ref=image_ref(rb.img): run_all([["docker", "rmi", ref]]), rb.hint,
                          kind="ponto_de_volta"))

    small_pullable = []
    for img in sorted(d.images, key=lambda i: -i["size"]):
        if img["used"] or img["origin"] == "none" or id(img) in rollbacks:
            continue
        local = img["origin"] == "local"
        if not local and img["size"] < BIG_ITEM:
            small_pullable.append(img)
            continue
        items.append(Item("docker", f"Imagem {img['name']}", img["size"], LOSS if local else REDOWN,
                          lambda ref=image_ref(img): run_all([["docker", "rmi", ref]]),
                          f"{ORIGIN_LABEL[img['origin']]} · {img['created']}"))
    if small_pullable:
        names = [i["name"] for i in small_pullable]
        refs = [image_ref(i) for i in small_pullable]
        items.append(Item("docker", f"Imagens pequenas de registry/Hub ({len(names)})",
                          sum(i["size"] for i in small_pullable), REDOWN,
                          lambda refs=refs: run_all([["docker", "rmi", r] for r in refs]),
                          preview([n.rsplit("/", 1)[-1] for n in names])))
    return items


# ---------------------------------------------------------------- worktrees

@dataclass
class Worktree:
    path: Path
    repo: Path
    head: str = ""
    branch: str = ""
    size: int = 0
    last_activity: float = 0
    reason: str = "não verificado"
    eligible: bool = False
    merged: str = ""                                       # "squash em origin/main"; vazio = não comprovado
    pushed: str = ""                                       # "origin/feature" contém o HEAD; vazio = não comprovado
    users: list[str] = field(default_factory=list)         # quem usa: sessões, processos, containers
    sessions: list[str] = field(default_factory=list)      # sessões vivas do Claude Code que trabalham nele
    blockers: list[str] = field(default_factory=list)      # ⚪ manter: trabalho que só existe aqui ou dúvida
    waits: list[str] = field(default_factory=list)         # 🟡 depois: em uso ou mexido há pouco
    conversations: list[ConvLink] = field(default_factory=list)  # conversas do Claude Code ligadas a ele
    checks: list[tuple[bool, str]] = field(default_factory=list)  # cada verificação: (passou, texto)

    @property
    def verdict(self) -> str:
        return "seguro" if self.eligible else ("manter" if self.blockers or not self.waits else "depois")


def branch_name(w: Worktree) -> str:
    """refs/heads/feat/x → feat/x."""
    return w.branch.removeprefix("refs/heads/")


def git_at(path: Path, *args: str) -> subprocess.CompletedProcess | None:
    return sh(["git", "--no-optional-locks", "-C", str(path), *args])


def inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def worktree_usage() -> tuple[list[tuple[Path, str]], str, list[tuple[Path, str]]]:
    """Arquivos abertos (caminho, processo) e mounts de containers rodando (origem, container).

    Uma falha de inspeção nunca equivale a ausência de uso."""
    r = sh(["lsof", "-Fpcn"], timeout=60)
    if r is None or r.returncode != 0 or r.stderr.strip():
        return [], "não foi possível verificar arquivos abertos (lsof)", []
    claude = {str(d["pid"]): d["name"] for d in live_claude_sessions()[0]}
    paths, pid, cmd = [], "", ""
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            pid, cmd = line[1:], ""
        elif line.startswith("c"):
            cmd = line[1:]
        elif line.startswith("n/"):
            who = f"Claude Code {claude[pid]}" if pid in claude else f"{cmd or 'processo'} (pid {pid})"
            paths.append((Path(line[1:]).resolve(), who))
    mounts = []
    if shutil.which("docker"):
        r = sh(["docker", "ps", "-q"])
        if r is None or r.returncode:
            return paths, "não foi possível verificar os containers Docker", []
        ids = r.stdout.split()
        if ids:
            r = sh(["docker", "inspect", "--format", "{{.Name}}\t{{json .Mounts}}", *ids])
            if r is None or r.returncode:
                return paths, "não foi possível verificar os mounts Docker", []
            try:
                for line in r.stdout.splitlines():
                    name, _, raw = line.partition("\t")
                    mounts.extend((Path(m["Source"]).resolve(), f"container {name.lstrip('/')}")
                                  for m in json.loads(raw) if m.get("Source"))
            except (ValueError, TypeError, KeyError):
                return paths, "resposta de mounts Docker inválida", []
    return paths, "", mounts


def registered_worktrees(repo: Path) -> list[dict[str, str]]:
    r = git_at(repo, "worktree", "list", "--porcelain", "-z")
    if r is None or r.returncode:
        raise RuntimeError(f"não consegui listar worktrees de {repo}")
    records, record = [], {}
    for field in r.stdout.split("\0"):
        if not field:
            if record:
                records.append(record)
                record = {}
        else:
            key, _, value = field.partition(" ")
            record[key] = value
    return records


def registered_dirs(records: list[dict[str, str]]) -> list[Path]:
    """Pastas registradas de um repositório (a principal incluída); registro sem `worktree` fica de fora."""
    return [Path(r["worktree"]).resolve() for r in records if "worktree" in r]


# Pastas ignoradas que um build ou instalação recria. Qualquer outro ignorado (.env, estado Terraform,
# planos de sessões de agentes, certificados) bloqueia a remoção.
REGENERABLE = frozenset({
    "node_modules", "target", "build", "dist", ".docusaurus", ".angular", ".next", ".nuxt", ".svelte-kit",
    ".vite", ".turbo", ".parcel-cache", "coverage", ".nyc_output", ".gradle", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv", ".DS_Store",
})

def raise_error(error: OSError) -> None:
    raise error


def only_regenerable(folder: Path) -> bool:
    for root, dirs, files in os.walk(folder, onerror=raise_error):
        dirs[:] = [d for d in dirs if d not in REGENERABLE]
        if any(f not in REGENERABLE for f in files):
            return False
    return True


def protected_ignored(listing: str, path: Path) -> list[str]:
    """Ignorados (saída -z de ls-files --directory) que não são artefato regenerável.

    Uma pasta inteira ignorada aparece como uma entrada só ("web/"); ela passa se tudo dentro dela estiver
    em pastas regeneráveis ("web/node_modules")."""
    kept = []
    for entry in filter(None, listing.split("\0")):
        if entry.rstrip("/").rsplit("/", 1)[-1] in REGENERABLE:
            continue
        if entry.endswith("/") and only_regenerable(path / entry):
            continue
        if not any(entry.startswith(k) for k in kept if k.endswith("/")):
            kept.append(entry)
    return kept


def default_branch(path: Path, remote: str = "origin") -> str:
    """Branch padrão do remoto nas referências locais: `<remoto>/HEAD`, senão `<remoto>/main`, senão `/master`."""
    r = git_at(path, "symbolic-ref", f"refs/remotes/{remote}/HEAD")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    for ref in (f"refs/remotes/{remote}/main", f"refs/remotes/{remote}/master"):
        r = git_at(path, "rev-parse", "--verify", "--quiet", ref)
        if r is not None and r.returncode == 0:
            return ref
    return ""


def patch_ids(path: Path, *args: str) -> set[str] | None:
    """patch-id de cada commit/diff da saída de `git <args>`; só leitura."""
    diff = git_at(path, *args, "--no-color", "--no-ext-diff")
    if diff is None or diff.returncode:
        return None
    try:
        r = subprocess.run(["git", "-C", str(path), "patch-id", "--stable"], input=diff.stdout,
                           capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return {line.split()[0] for line in r.stdout.splitlines() if line.strip()} if r.returncode == 0 else None


def merged_how(path: Path, head: str, ref: str) -> str:
    """'merge', 'rebase' ou 'squash' quando o conteúdo do HEAD comprovadamente está em ref; senão ''."""
    r = git_at(path, "merge-base", "--is-ancestor", head, ref)
    if r is not None and r.returncode == 0:
        return "merge"
    mb = git_at(path, "merge-base", head, ref)
    if mb is None or mb.returncode or not mb.stdout.strip():
        return ""
    base = mb.stdout.strip()
    cherry = git_at(path, "cherry", ref, head, base)
    if cherry is not None and cherry.returncode == 0:
        lines = cherry.stdout.splitlines()
        if lines and all(line.startswith("- ") for line in lines):
            return "rebase"
    mine = patch_ids(path, "diff", base, head)
    if not mine or len(mine) != 1:
        return ""
    theirs = patch_ids(path, "log", "--no-merges", "-p", "--max-count=2000", f"{base}..{ref}")
    return "squash" if theirs and mine <= theirs else ""


def pushed_ref(path: Path, head: str, branch: str) -> str | None:
    """Ref remota local (sem refs/remotes/) que contém o HEAD: a da própria branch, senão a primeira.

    Só lê o que o último fetch/push deixou; nunca acessa a rede. '' = nenhuma; None = o Git não respondeu."""
    r = git_at(path, "for-each-ref", "--contains", head, "--format=%(refname)", "refs/remotes")
    if r is None or r.returncode:
        return None
    refs = [x.removeprefix("refs/remotes/") for x in r.stdout.split() if not x.endswith("/HEAD")]
    own = branch.removeprefix("refs/heads/")
    return next((x for x in refs if x.partition("/")[2] == own), refs[0] if refs else "")


def claude_key(path: Path | str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def claude_dir() -> Path:
    """Pasta do Claude Code: CLAUDE_CONFIG_DIR quando definida (o Claude Code a respeita), senão ~/.claude.
    O hogs só lê ali."""
    custom = os.environ.get("CLAUDE_CONFIG_DIR", "")
    return Path(custom).expanduser() if custom else HOME / ".claude"


@dataclass
class Agents:
    """Sinais de sessões de agentes (Claude Code, Codex) sobre os worktrees."""
    folders: list[tuple[str, str]] = field(default_factory=list)  # (agente, pasta ou chave do Claude)
    mentions: dict[Path, set[str]] = field(default_factory=dict)  # worktree → sessões que o citaram
    error: str = ""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_claude_sessions() -> tuple[list[dict], str]:
    """Registros de ~/.claude/sessions/<pid>.json cujo processo está vivo. Registro que sumiu entre a listagem
    e a leitura é de sessão que acabou de fechar: não conta."""
    registry = claude_dir() / "sessions"
    found = []
    for entry in (registry.glob("*.json") if registry.is_dir() else []):
        try:
            data = json.loads(entry.read_text())
        except FileNotFoundError:
            continue
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return [], f"registro de sessão do Claude Code ilegível: {entry.name}"
        if isinstance(data.get("pid"), int) and pid_alive(data["pid"]):
            data["name"] = str(data.get("name") or f"pid {data['pid']}")
            found.append(data)
    return found, ""


def live_claude_transcripts() -> tuple[list[tuple[str, Path]], str]:
    """Transcrições das sessões do Claude Code com processo vivo.

    O Claude Code registra cada sessão aberta em ~/.claude/sessions/<pid>.json (sessionId, cwd, startedAt) e
    grava a conversa em ~/.claude/projects/<cwd com '-'>/<sessionId>.jsonl, com subagentes em <sessionId>/.
    Só a conversa atual conta: outras transcrições da mesma pasta são de outras sessões.
    Uma sessão viva sem transcrição localizável torna a verificação inconclusiva. Devolve (nome, arquivo)."""
    projects = claude_dir() / "projects"
    files: set[tuple[str, Path]] = set()
    sessions, error = live_claude_sessions()
    if error:
        return [], error
    for data in sessions:
        sid, name = data.get("sessionId"), data["name"]
        transcripts = list(projects.glob(f"*/{sid}.jsonl")) if sid else []
        if not transcripts:
            return [], f"sessão ativa do Claude Code sem transcrição localizável (pid {data['pid']})"
        for m in transcripts:
            files.add((name, m))
            if m.with_suffix("").is_dir():
                files.update((name, f) for f in m.with_suffix("").rglob("*.jsonl"))
    return sorted(files), ""


def claude_work(line: bytes) -> bytes:
    """Parte de uma linha da transcrição do Claude Code que é trabalho na sessão (ver claude_work_record).
    Linha ilegível conta inteira."""
    try:
        return claude_work_record(json.loads(line))
    except (ValueError, AttributeError):
        return line


def claude_work_record(record: dict) -> bytes:
    """O que o assistente fez ou escreveu (comandos, caminhos, texto) e o que o usuário digitou, num registro já
    lido. Saída de ferramenta (tool_result, como `git worktree list`) e anexos de contexto não contam.
    Registro fora do formato levanta AttributeError."""
    kind, content = record.get("type"), (record.get("message") or {}).get("content")
    if kind == "assistant":
        return json.dumps(content, ensure_ascii=False).encode()
    if kind == "user":
        if isinstance(content, str):
            return content.encode()
        if isinstance(content, list):
            return json.dumps([c for c in content if not (isinstance(c, dict) and c.get("type") == "tool_result")],
                              ensure_ascii=False).encode()
    return b""


CONVERSATIONS_CACHE = DATA_DIR / "conversations.json"
CONVERSATIONS_CACHE_VERSION = 2   # 2: escopo citado sem diferenciar maiúsculas
MENTION_PARTS = 8
MENTION_END = re.compile(rb"[^\"'`\\\n\t<>|;&()\[\]{}]*")


@dataclass
class Conversation:
    """Uma conversa do Claude Code (transcrição principal + subagentes)."""
    session: str
    title: str = ""
    start: str = ""                                                   # primeiro cwd: de onde `--resume` a acha
    last: float = 0.0
    cwds: set[str] = field(default_factory=set)
    branches: set[tuple[str, str]] = field(default_factory=set)       # (cwd, gitBranch)
    mentions: set[str] = field(default_factory=set)                   # caminhos citados, relativos ao escopo
    live: bool = False


@dataclass
class ConvLink:
    conv: Conversation
    how: str        # "dentro", "branch" ou "citou"
    reopen: bool    # a conversa começou dentro do worktree


def scope_needle(scope: Path) -> re.Pattern[bytes]:
    """`<escopo>/` sem diferenciar maiúsculas (ASCII), como o APFS."""
    return re.compile(re.escape(bytes(scope) + b"/"), re.IGNORECASE)


def mention_tails(work: bytes, scope: Path) -> set[str]:
    """Caminhos citados abaixo de `scope`, relativos a ele e cortados em MENTION_PARTS partes. Espaço não
    termina o caminho (pastas com espaço); aspas, barra invertida, quebra de linha, operadores de shell e a
    próxima citação do escopo sim (`cp <escopo>/a <escopo>/b` cita os dois). O escopo casa sem diferenciar
    maiúsculas; o resto fica como foi escrito (conversation_links compara sem diferenciar)."""
    needle = scope_needle(scope)
    found: set[str] = set()
    at = needle.search(work)
    while at:
        begin = at.end()
        at = needle.search(work, begin)
        m = MENTION_END.match(work, begin, at.start() if at else len(work))
        tail = m.group().decode("utf-8", errors="replace").rstrip(" .,:")
        tail = "/".join(tail.split("/")[:MENTION_PARTS])
        if tail:
            found.add(tail)
    return found


def parse_iso(ts: str) -> float | None:
    """Data ISO das transcrições ("2026-11-04T12:34:56.789Z") em epoch; ilegível → None."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def read_transcript(path: Path, entry: dict | None, scope: Path) -> dict:
    """Entrada de cache de uma transcrição. Se `entry` ainda vale (mesmo inode, arquivo não encolheu e os bytes
    antes de `offset` batem com `tail`), lê só o que foi acrescentado; senão lê do início. Só linhas completas."""
    st = path.stat()
    sub = path.parent.name == "subagents"
    # Filtro rápido só quando o escopo aparece igual no JSON (ASCII sem escapes); senão toda linha é lida.
    quick = scope_needle(scope) if json.dumps(str(scope) + "/")[1:-1].encode() == bytes(scope) + b"/" else None
    with path.open("rb") as f:
        state = None
        try:
            offset, tail = entry["offset"], bytes.fromhex(entry["tail"])
            if entry["ino"] == st.st_ino and isinstance(offset, int) and len(tail) <= offset <= st.st_size:
                f.seek(offset - len(tail))
                if f.read(len(tail)) == tail:
                    state = (offset, str(entry["start"]), str(entry["title"]), float(entry["last"]),
                             {str(c) for c in entry["cwds"]}, {(str(c), str(b)) for c, b in entry["branches"]},
                             {str(m) for m in entry["mentions"]})
        except (TypeError, KeyError, ValueError, AttributeError):  # sem cache ou cache fora do formato
            pass
        offset, start, title, last, cwds, branches, mentions = state or (0, "", "", 0.0, set(), set(), set())
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind, cwd, branch = record.get("type"), record.get("cwd"), record.get("gitBranch")
            if isinstance(cwd, str) and cwd:
                start = start or cwd
                cwds.add(cwd)
                if isinstance(branch, str) and branch and branch != "HEAD":
                    branches.add((cwd, branch))
            if kind == "ai-title" and isinstance(record.get("aiTitle"), str):
                title = record["aiTitle"]
            if kind in ("user", "assistant"):
                ts = parse_iso(record.get("timestamp"))
                if ts is not None:
                    last = max(last, ts)
            if quick is None or quick.search(line):
                try:
                    work = claude_work_record(record)
                except AttributeError:
                    work = line
                mentions |= mention_tails(work, scope)
        f.seek(max(0, offset - 32))
        tail = f.read(offset - max(0, offset - 32))
    return {"ino": st.st_ino, "offset": offset, "tail": tail.hex(),
            "session": path.parent.parent.name if sub else path.stem, "main": not sub, "start": start,
            "title": title, "last": last, "cwds": sorted(cwds), "branches": sorted(branches),
            "mentions": sorted(mentions)}


def claude_conversations(scope: Path, projects: Path | None = None,
                         cache: Path | None = None) -> tuple[list[Conversation], str]:
    """Todas as conversas do Claude Code (~/.claude/projects), com cache incremental: cada transcrição é lida
    só a partir de onde parou na última vez. Menções são relativas a `scope`; trocar o escopo relê tudo."""
    projects = claude_dir() / "projects" if projects is None else projects
    cache = CONVERSATIONS_CACHE if cache is None else cache
    if not projects.is_dir():
        return [], ""
    saved = read_json(cache)
    same = saved.get("v") == CONVERSATIONS_CACHE_VERSION and saved.get("scope") == str(scope)
    known = saved.get("files") if same else None
    known = known if isinstance(known, dict) else {}
    entries: dict[str, dict] = {}
    try:
        for f in sorted([*projects.glob("*/*.jsonl"), *projects.glob("*/*/subagents/*.jsonl")]):
            try:
                entries[str(f)] = read_transcript(f, known.get(str(f)), scope)
            except FileNotFoundError:
                continue
        by: dict[str, list[tuple[str, dict]]] = {}
        for f, e in entries.items():
            by.setdefault(e["session"], []).append((f, e))
        sessions, live_error = live_claude_sessions()
        live = {s.get("sessionId") for s in sessions}
        convs = []
        for session, files in by.items():
            mains = [(f, e) for f, e in files if e["main"]]
            if not mains:
                continue
            c = Conversation(session, live=session in live)
            for f, e in files:
                c.last = max(c.last, e["last"])
                c.cwds.update(e["cwds"])
                c.branches.update((cwd, b) for cwd, b in e["branches"])
                c.mentions.update(e["mentions"])
            for f, e in mains:
                c.start = c.start or e["start"]
                c.title = e["title"] or c.title
            if not c.last:
                try:
                    c.last = Path(mains[0][0]).stat().st_mtime
                except FileNotFoundError:
                    pass
            convs.append(c)
    except OSError as exc:
        return [], f"não foi possível ler as conversas do Claude Code: {exc}"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"v": CONVERSATIONS_CACHE_VERSION, "scope": str(scope), "files": entries}))
        os.replace(tmp, cache)
    except OSError:
        pass
    error = f"não foi possível verificar as sessões abertas do Claude Code: {live_error}" if live_error else ""
    return sorted(convs, key=lambda c: c.last, reverse=True), error


def resume_command(c: Conversation) -> str:
    if not c.start:
        return f"claude --resume {c.session}"
    return f"cd {shlex.quote(c.start)} && claude --resume {c.session}"


def codex_work(line: bytes) -> bytes:
    """Mesma ideia de claude_work para ~/.codex/sessions: chamadas de ferramenta (comando, patch) e mensagens
    do usuário e do assistente contam; saídas (*_output, event_msg) e contexto do sistema não."""
    try:
        record = json.loads(line)
        payload = record.get("payload") or {}
        kind = payload.get("type") if record.get("type") == "response_item" else None
    except (ValueError, AttributeError):
        return line
    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        return json.dumps([payload.get(k) for k in ("arguments", "input", "action")], ensure_ascii=False).encode()
    if kind == "message" and payload.get("role") in ("user", "assistant"):
        return json.dumps(payload.get("content"), ensure_ascii=False).encode()
    return b""


def agent_activity(days: int, paths: list[Path]) -> Agents:
    """Sessões que estão ou estiveram trabalhando nos worktrees.

    - Claude Code vivo: o caminho absoluto do worktree num comando, arquivo ou texto da conversa atual
      (inclusive subagentes) bloqueia, sem prazo.
    - Codex nos últimos `days` dias: cwd dentro do worktree, ou o caminho num comando ou mensagem.
    - Claude Code nos últimos `days` dias: pasta de trabalho dentro do worktree (~/.claude/projects)."""
    since = time.time() - days * 86400
    agents = Agents()
    projects, codex = claude_dir() / "projects", HOME / ".codex" / "sessions"
    scan: list[tuple[str, Path, Callable[[bytes], bytes]]] = []
    try:
        for d in (projects.iterdir() if projects.is_dir() else []):
            if any(f.stat().st_mtime > since for f in d.glob("*.jsonl")):
                agents.folders.append(("Claude Code", d.name))
        live, error = live_claude_transcripts()
        if error:
            agents.error = error
            return agents
        scan += [(f"Claude Code {name}", f, claude_work) for name, f in live]
        for f in (codex.rglob("*.jsonl") if codex.is_dir() else []):
            if f.stat().st_mtime <= since:
                continue
            with f.open(encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            try:
                cwd = json.loads(first).get("payload", {}).get("cwd")
            except (ValueError, AttributeError):
                agents.error = f"sessão do Codex ilegível: {short(f)}"
                return agents
            if cwd:
                agents.folders.append(("Codex", str(Path(cwd).resolve())))
            scan.append(("Codex", f, codex_work))
        if paths and scan:
            alts = sorted({str(p).encode() for p in paths}, key=len, reverse=True)
            rx = re.compile(b"(" + b"|".join(re.escape(a) for a in alts) + rb")(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])")
            for label, f, work in scan:
                data = b"\n".join(work(line) for line in f.read_bytes().splitlines() if rx.search(line))
                for hit in set(rx.findall(data)):
                    agents.mentions.setdefault(Path(hit.decode()), set()).add(label)
    except OSError as exc:
        agents.error = f"não foi possível verificar sessões de agentes: {exc}"
    return agents


def session_matches(key: str, path: Path) -> bool:
    """Sessão aberta na pasta ou dentro dela. A chave do Claude é ambígua (pasta-x casa com pasta/x);
    o erro vai para o lado de preservar."""
    if key.startswith("/"):
        return inside(Path(key), path)
    own = claude_key(path)
    return key == own or key.startswith(own + "-")


def group_processes(labels: set[str]) -> list[str]:
    """{"node (pid 1)", "node (pid 2)", "vim (pid 3)"} → ["node ×2", "vim (pid 3)"]; sessões já saíram antes."""
    by: dict[str, list[str]] = {}
    for label in labels:
        if label.startswith("Claude Code "):
            continue
        m = re.fullmatch(r"(.*) \(pid (\d+)\)", label)
        by.setdefault(m.group(1) if m else label, []).append(label)
    return [items[0] if len(items) == 1 else f"{cmd} ×{len(items)}" for cmd, items in sorted(by.items())]


def elapsed(ts: float) -> str:
    secs = max(0, time.time() - ts)
    if secs < 3600:
        return f"há {int(secs // 60)} min"
    if secs < 86400:
        return f"há {int(secs // 3600)} h"
    return f"há {int(secs // 86400)} d"


def last_change(path: Path, gitdir: Path) -> float:
    """Última modificação do que é trabalho: arquivos fora de pastas regeneráveis e metadados Git
    (commit, checkout, add). Builds e instalações em node_modules/target não contam, nem o mtime de pastas
    (muda quando um build cria node_modules); arquivo versionado apagado aparece no git status, e um
    processo rodando na pasta aparece pelo lsof."""
    stamps = [(gitdir / n).stat().st_mtime for n in ("logs/HEAD", "index", "HEAD") if (gitdir / n).exists()]
    for root, dirs, files in os.walk(path, onerror=raise_error):
        dirs[:] = [d for d in dirs if d != ".git" and d not in REGENERABLE]
        stamps += [(Path(root) / f).lstat().st_mtime for f in files if f != ".git" and f not in REGENERABLE]
    return max(stamps, default=0)


SESSION_WINDOW_DAYS = 7  # janela das sessões do Codex e das pastas do Claude mostradas como informação


def inspect_worktree(w: Worktree, days: int, usage: tuple[list[tuple[Path, str]], str, list[tuple[Path, str]]],
                     agents: Agents | None = None, links: list[ConvLink] | None = None, conv_error: str = "",
                     stale_days: int = DEFAULTS["stale_worktree_days"]) -> Worktree:
    """Coleta todos os sinais e decide: 🟢 seguro, 🟡 depois (em uso ou recente) ou ⚪ manter.

    🟢 é mergeado na principal, ou sem merge mas enviado (HEAD numa ref remota local) e parado há mais de
    stale_days, sem conversa do Claude Code nesse prazo. links=None = conversas não conferidas (bloqueia o
    caminho sem merge); [] = conferidas, nenhuma ligada. Cada verificação feita vira uma linha de checks."""
    result = Worktree(w.path, w.repo, size=w.size, conversations=list(links or []))
    marks = [0, 0]
    def check(ok_text: str) -> None:
        """Fecha uma verificação: falhou se ela acrescentou blocker ou wait desde a anterior."""
        new = result.blockers[marks[0]:] + result.waits[marks[1]:]
        result.checks.append((not new, new[0] if new else ok_text))
        marks[:] = [len(result.blockers), len(result.waits)]
    def keep(reason: str) -> Worktree:
        result.blockers.append(reason)
        result.reason = result.blockers[0]
        return result
    try:
        records = registered_worktrees(w.repo)
        entry = next((r for r in records if Path(r["worktree"]).resolve() == w.path), None)
        if not entry or entry is records[0] or not (w.path / ".git").is_file():
            return keep("checkout principal, ausente ou não registrado")
        result.head, result.branch = entry.get("HEAD", ""), entry.get("branch", "")
        if any(k in entry for k in ("locked", "prunable", "bare", "detached")):
            return keep("worktree bloqueado, ausente ou HEAD destacado")
        if any(inside(Path(r["worktree"]).resolve(), w.path) and Path(r["worktree"]).resolve() != w.path
               for r in records):
            return keep("contém outro worktree")

        # ⚪ trabalho que só existe aqui, ou dúvida
        b = result.blockers
        status = git_at(w.path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
        if status is None or status.returncode:
            b.append("não foi possível verificar alterações locais")
        elif status.stdout:
            b.append(f"{len(status.stdout.splitlines())} alterações locais ou arquivos não versionados")
        if (w.path / ".gitmodules").exists():
            b.append("contém submódulos; revise antes")
        check("tudo commitado")
        ref = default_branch(w.path)
        base_name = ref.removeprefix("refs/remotes/").partition("/")[2]
        if not ref:
            b.append("branch principal remota desconhecida (origin/HEAD)")
        else:
            how = merged_how(w.path, result.head, ref)
            if how:
                result.merged = f"{how} em {ref.removeprefix('refs/remotes/')}"
            else:
                pushed = pushed_ref(w.path, result.head, result.branch)
                if pushed is None:
                    b.append("não foi possível verificar se os commits foram enviados")
                elif not pushed:
                    b.append(f"merge não comprovado em {ref.removeprefix('refs/remotes/')} "
                             "e há commits não enviados")
                else:
                    result.pushed = pushed
        check(f"já na {base_name} ({result.merged})" if result.merged
              else f"enviado a {result.pushed} (conferido nas referências locais de "
                   f"{result.pushed.partition('/')[0]})")
        ignored = git_at(w.path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        if ignored is None or ignored.returncode:
            b.append("não foi possível verificar arquivos ignorados")
        else:
            kept = protected_ignored(ignored.stdout, w.path)
            if kept:
                more = f" (+{len(kept) - 3})" if len(kept) > 3 else ""
                b.append(f"ignorados que não voltam pelo Git: {', '.join(kept[:3])}{more}")
        check("sem .env nem arquivo que o Git não recupera")

        # Quem usa a pasta. Sessões do Claude Code e do Codex que trabalharam nele são só informação: o
        # trabalho delas já está na main. Trava apenas o que roda dentro da pasta agora (dev server,
        # container, Claude aberto dentro dela), porque apagar a pasta derruba o programa.
        paths, error, mounts = usage
        if error:
            b.append(error)
        if agents is None:
            agents = agent_activity(SESSION_WINDOW_DAYS, [w.path])
        cited = agents.mentions.get(w.path, set())
        running = {who for p, who in paths if inside(p, w.path)}
        opened_inside = sorted(x.removeprefix("Claude Code ") for x in running if x.startswith("Claude Code "))
        result.sessions = sorted({c.removeprefix("Claude Code ") for c in cited if c.startswith("Claude Code ")}
                                 | set(opened_inside))
        containers = sorted({who for p, who in mounts if inside(p, w.path) or inside(w.path, p)})
        result.users = [f"Claude Code {n}" + (" (aberta dentro)" if n in opened_inside else "")
                        for n in result.sessions]
        if "Codex" in cited:
            result.users.append(f"Codex (últimos {SESSION_WINDOW_DAYS} d)")
        for name in sorted({name for name, key in agents.folders if session_matches(key, w.path)}):
            if name == "Codex" or not result.sessions:
                result.users.append(f"{name} aberto na pasta (últimos {SESSION_WINDOW_DAYS} d)")
        result.users += group_processes(running) + containers
        if agents.error:
            result.users.append(f"sessões: {agents.error}")
        now_here = ([f"Claude Code {n} aberta dentro da pasta" for n in opened_inside]
                    + group_processes(running) + containers)
        if now_here:
            result.waits.append(f"em uso agora: {', '.join(now_here[:2])}")
        check("nada rodando dentro, nenhum container")

        gitdir = git_at(w.path, "rev-parse", "--absolute-git-dir")
        if gitdir is None or gitdir.returncode:
            b.append("metadados Git indisponíveis")
        else:
            result.last_activity = last_change(w.path, Path(gitdir.stdout.strip()))
            if days and result.last_activity > time.time() - days * 86400:
                result.waits.append(f"mexido {elapsed(result.last_activity)} (mínimo {days} d)")
        # Sem merge, a pasta só sai parada: nada mexido nem conversado no prazo, e nenhuma sessão viva nela.
        if not result.merged and result.pushed:
            since = time.time() - stale_days * 86400
            if result.last_activity > since:
                result.waits.append(f"sem merge, mexido {elapsed(result.last_activity)} "
                                    f"(em andamento; prazo {stale_days} d)")
            recent = [link.conv for link in result.conversations if link.conv.last > since]
            if recent:
                conv = max(recent, key=lambda c: c.last)
                result.waits.append(f"conversa do Claude Code {elapsed(conv.last)}: "
                                    f"{conv.title or conv.session[:8]}")
            if result.sessions:
                result.waits.append(f"conversa aberta do Claude Code: {', '.join(result.sessions)}")
            if links is None:
                b.append("conversas do Claude Code não verificadas")
            if conv_error:
                b.append(conv_error)
        idle = max(0, int((time.time() - result.last_activity) // 86400))
        check(f"sem mexer desde {datetime.fromtimestamp(result.last_activity):%d/%m} ({idle} dias)")
    except (OSError, RuntimeError, KeyError) as exc:
        result.blockers.append(f"inspeção inconclusiva: {exc}")
        check("")
    result.eligible = not result.blockers and not result.waits and bool(result.merged or result.pushed)
    if result.eligible and not result.merged:
        result.reason = f"enviado a {result.pushed} · mexido {elapsed(result.last_activity)} · a branch fica"
    elif result.eligible:
        result.reason = f"{result.merged} · mexido {elapsed(result.last_activity)}"
        if result.sessions:
            result.reason += f" · sessão {', '.join(result.sessions)}"
    else:
        result.reason = (result.blockers or result.waits or ["não verificado"])[0]
    return result


def find_repos(root: Path) -> tuple[list[Path], list[str]]:
    """Checkout principal de cada repositório achado sob root (uma vez por repositório), ordenado, e avisos.
    Não desce em pastas de dependências nem para dentro de um repositório."""
    warnings, mains, seen = [], set(), set()
    def walk_error(error: OSError) -> None:
        warnings.append(str(error))
    for directory, dirs, files in os.walk(root, onerror=walk_error):
        if ".git" in dirs or ".git" in files:
            repo = Path(directory)
            dirs[:] = []
            common = git_at(repo, "rev-parse", "--git-common-dir")
            if common is None or common.returncode:
                warnings.append(f"não consegui inspecionar {repo}")
                continue
            key = (repo / common.stdout.strip()).resolve()
            if key in seen:
                continue
            seen.add(key)
            try:
                mains.add(Path(registered_worktrees(repo)[0]["worktree"]).resolve())
            except (RuntimeError, KeyError, IndexError):
                mains.add(repo)
        else:
            dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "target", ".venv", "vendor"}]
    return sorted(mains), warnings


def fold(path: Path | str) -> str:
    """Caminho na forma em que o APFS compara: sem diferenciar maiúsculas nem a forma Unicode (NFC/NFD)."""
    return unicodedata.normalize("NFC", str(path)).casefold()


def inside_folded(path: str, parent: str) -> bool:
    """inside() para caminhos já passados por fold()."""
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def conversation_links(rows: list[Worktree], convs: list[Conversation], scope: Path,
                       repo_dirs: dict[Path, list[Path]]) -> dict[Path, list[ConvLink]]:
    """Conversas do Claude Code ligadas a cada worktree, da mais recente para a mais antiga:
    "dentro" (trabalhou numa pasta dentro dele), senão "branch" (trabalhou na branch dele em alguma pasta do
    mesmo repositório), senão "citou" (citou um caminho dele). `repo_dirs`: pastas registradas de cada
    repositório, a principal incluída. Menções são relativas a `scope`, com o limite de agent_activity
    (wt-a não casa com wt-a-2 nem com wt-a.bak).

    Como o APFS, nada aqui diferencia maiúsculas. Cada cwd vale como foi escrito e passando pelos symlinks
    (resolve, sem falhar se a pasta sumiu): basta uma das formas casar. Menções vêm cortadas em MENTION_PARTS
    partes: um worktree mais fundo casa pelos primeiros níveis (dois worktrees que só diferem depois disso casam
    os dois). Na dúvida, liga."""
    forms: dict[str, tuple[str, ...]] = {}
    def here(path: str) -> tuple[str, ...]:
        """As formas de um caminho da transcrição para comparar; nenhuma se ele não é absoluto."""
        if path not in forms:
            found: tuple[str, ...] = ()
            if path.startswith("/"):
                try:
                    found = tuple({fold(path), fold(Path(path).resolve())})
                except (OSError, RuntimeError):   # laço de symlinks: fica como foi escrito
                    found = (fold(path),)
            forms[path] = found
        return forms[path]

    folded_scope = fold(scope)
    rel: dict[str, list[Path]] = {}
    for w in rows:
        path = fold(w.path)
        if inside_folded(path, folded_scope) and path != folded_scope:   # fora do escopo: menções não o alcançam
            key = "/".join(path[len(folded_scope.rstrip("/")) + 1:].split("/")[:MENTION_PARTS])
            rel.setdefault(key, []).append(w.path)
    alts = sorted(rel, key=len, reverse=True)
    rx = (re.compile("^(" + "|".join(map(re.escape, alts)) + r")(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])")
          if alts else None)
    targets = [(w, fold(w.path), branch_name(w), [fold(d) for d in repo_dirs.get(w.repo, [])]) for w in rows]
    links: dict[Path, list[ConvLink]] = {w.path: [] for w in rows}
    for c in convs:
        cwds = [x for cwd in c.cwds for x in here(cwd)]
        branches = [(x, b) for cwd, b in c.branches for x in here(cwd)]
        cited = ({p for m in map(rx.match, map(fold, c.mentions)) if m for p in rel[m.group(1)]}
                 if rx else set())
        start = here(c.start)
        for w, path, own, dirs in targets:
            if any(inside_folded(x, path) for x in cwds):
                how = "dentro"
            elif own and any(b == own and any(inside_folded(cwd, d) for d in dirs) for cwd, b in branches):
                how = "branch"
            elif w.path in cited:
                how = "citou"
            else:
                continue
            links[w.path].append(ConvLink(c, how, any(inside_folded(x, path) for x in start)))
    for items in links.values():
        items.sort(key=lambda link: link.conv.last, reverse=True)
    return links


def linked_conversations(rows: list[Worktree], scope: Path,
                         repo_dirs: dict[Path, list[Path]]) -> tuple[dict[Path, list[ConvLink]], str]:
    """Lê as conversas do Claude Code agora e liga cada uma aos worktrees (e o erro da leitura, se houve).
    Sem worktree, nada é lido."""
    if not rows:
        return {}, ""
    convs, error = claude_conversations(scope)
    return conversation_links(rows, convs, scope, repo_dirs), error


def gather_worktrees(root: Path, days: int) -> tuple[list[Worktree], list[str]]:
    root = root.expanduser().resolve()
    repos, warnings = find_repos(root)
    found: dict[Path, Worktree] = {}
    repo_dirs: dict[Path, list[Path]] = {}
    for repo in repos:
        try:
            records = registered_worktrees(repo)
        except RuntimeError as exc:
            warnings.append(str(exc))
            continue
        repo_dirs[repo] = registered_dirs(records)
        for record in records[1:]:
            if "worktree" not in record:
                continue
            path = Path(record["worktree"]).resolve()
            if inside(path, root):
                found[path] = Worktree(path, repo, head=record.get("HEAD", ""), branch=record.get("branch", ""))
    usage = worktree_usage() if found else ([], "", [])
    agents = agent_activity(SESSION_WINDOW_DAYS, list(found))
    links, conv_error = linked_conversations(list(found.values()), root, repo_dirs)
    stale_days = setting("stale_worktree_days")
    def inspect(w: Worktree) -> Worktree:
        w.size = du(w.path)
        return inspect_worktree(w, days, usage, agents, links=links[w.path], conv_error=conv_error,
                                stale_days=stale_days)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(inspect, found.values()))
    return sorted(rows, key=lambda w: -w.size), warnings


def remove_worktree(w: Worktree, days: int, scope: Path | None = None) -> None:
    """Reconfere tudo, inclusive conversas lidas agora, e remove sem --force; a branch fica."""
    scope = (scope or projects_root() or w.path.parent).expanduser().resolve()
    links, conv_error = linked_conversations([w], scope, {w.repo: registered_dirs(registered_worktrees(w.repo))})
    current = inspect_worktree(w, days, worktree_usage(), links=links[w.path], conv_error=conv_error,
                               stale_days=setting("stale_worktree_days"))
    if not current.eligible or (current.head, current.branch) != (w.head, w.branch):
        raise RuntimeError(current.reason if not current.eligible else "HEAD ou branch mudou desde a análise")
    r = git_at(w.repo, "worktree", "remove", str(w.path))
    if r is None or r.returncode:
        raise RuntimeError(r.stderr.strip() if r is not None else "Git não respondeu")


VERDICTS = {"seguro": ("🟢", "green"), "depois": ("🟡", "yellow"), "manter": ("⚪", "dim")}
# Como cada veredito aparece na tela; as chaves continuam as internas.
VERDICT_LABELS = {"seguro": "seguro", "depois": "em andamento", "manter": "com trabalho só aqui"}


def safe_worktrees(rows: list[Worktree]) -> list[Worktree]:
    """Os 🟢 na ordem em que recebem número (#1, #2…), a mesma no quadro e no --clean."""
    return sorted((w for w in rows if w.eligible), key=lambda w: (w.repo.name.lower(), -w.size))


def where(w: Worktree, root: Path | None) -> str:
    try:
        return str(w.path.relative_to(root)) if root else short(w.path)
    except ValueError:
        return short(w.path)


def hogs_command(root: Path, days: int) -> str:
    extra = f" --worktree-days {days}" if days else ""
    return f"hogs --worktrees {short(root)}{extra}"


def merge_cell(w: Worktree) -> Text:
    """Coluna da branch principal no quadro e no --clean: merge comprovado, só enviado ou nada comprovado."""
    if w.merged:
        return Text(f"✓ {w.merged}", style="green")
    if w.pushed:
        return Text("✗ sem merge", style="dim").append(f"\n✓ enviado ({w.pushed})", style="yellow")
    return Text("✗ não comprovado", style="dim")


def conversation_cell(w: Worktree) -> Text:
    """A conversa mais recente ligada ao worktree; embaixo, há quanto tempo, quantas outras e o aviso de reabrir."""
    if not w.conversations:
        return Text("—")
    last = max(w.conversations, key=lambda link: link.conv.last).conv
    head = ("● " if last.live else "") + (last.title or "(sem título)")
    foot = elapsed(last.last)
    if len(w.conversations) > 1:
        foot += f" · +{len(w.conversations) - 1}"
    if any(link.reopen for link in w.conversations):
        foot += " · ⚠ reabra antes de apagar"
    return Text(head).append(f"\n{foot}", style="dim")


def worktrees_report(con: Console, rows: list[Worktree], root: Path | None = None, days: int = 0,
                     stale_days: int = DEFAULTS["stale_worktree_days"]) -> None:
    """Quadro por projeto e, por último (o que fica na tela), o resumo."""
    order = {"seguro": 0, "depois": 1, "manter": 2}
    number = {id(w): n for n, w in enumerate(safe_worktrees(rows), 1)}
    projects: dict[Path, list[Worktree]] = {}
    for w in rows:
        projects.setdefault(w.repo, []).append(w)
    for repo in sorted(projects, key=lambda r: r.name.lower()):
        items = sorted(projects[repo], key=lambda w: (order[w.verdict], -w.size))
        t = Table(title=f"📁 {repo.name}  [dim]{short(repo)}[/]", title_justify="left",
                  box=box.SIMPLE_HEAD, expand=True)
        t.add_column("Worktree / branch", ratio=3)
        t.add_column("Tamanho", justify="right", no_wrap=True)
        t.add_column("Mudou", no_wrap=True)
        t.add_column("Na main?", ratio=2)
        t.add_column("Em uso por", ratio=2)
        t.add_column("Conversa", ratio=2, overflow="fold")
        t.add_column("Veredito", ratio=3, overflow="fold")   # "ellipsis" corta a palavra depois de um emoji (rich)
        for w in items:
            icon, style = VERDICTS[w.verdict]
            verdict = (f"{icon} seguro · #{number[id(w)]}" if w.eligible
                       else f"{icon} {VERDICT_LABELS[w.verdict]}: {w.reason}")
            if w.eligible and not w.merged:
                verdict += "\na pasta sai, a branch fica"
            t.add_row(Text(where(w, root)).append(f"\n{branch_name(w)}", style="dim"),
                      fmt(w.size),
                      elapsed(w.last_activity) if w.last_activity else "—",
                      merge_cell(w),
                      Text("\n".join(w.users) if w.users else "—"),
                      conversation_cell(w),
                      Text(verdict, style=style))
        con.print(t)
    worktrees_summary(con, rows, root, days, stale_days)


CATEGORIES = {
    "dentro": ("tem uma sessão do Claude Code aberta dentro da pasta",
               "têm uma sessão do Claude Code aberta dentro da pasta"),
    "aberta": ("tem uma sessão do Claude Code aberta que trabalha nele",
               "têm uma sessão do Claude Code aberta que trabalha neles"),
    "uso": ("tem algo rodando dentro (dev server, container)", "têm algo rodando dentro (dev server, container)"),
    "recente": ("foi mexido dentro do prazo pedido", "foram mexidos dentro do prazo pedido"),
    "andamento": ("está sem merge e foi mexido nos últimos {d} dias",
                  "estão sem merge e foram mexidos nos últimos {d} dias"),
    "conversa": ("está sem merge e tem conversa do Claude Code nos últimos {d} dias",
                 "estão sem merge e têm conversa do Claude Code nos últimos {d} dias"),
    "merge": ("tem commits que não estão na branch principal nem foram enviados",
              "têm commits que não estão na branch principal nem foram enviados"),
    "alterações": ("tem alterações ou arquivos novos sem commit", "têm alterações ou arquivos novos sem commit"),
    "ignorados": ("tem arquivos fora do Git que não voltam (.env, planos…)",
                  "têm arquivos fora do Git que não voltam (.env, planos…)"),
    "outros": ("está travado, sem branch ou não deu pra conferir",
               "estão travados, sem branch ou não deu pra conferir"),
}


def worktree_category(w: Worktree) -> str:
    """Motivo principal, para o resumo (chave de CATEGORIES)."""
    if w.verdict == "depois":
        first = w.waits[0]
        if first.startswith("sem merge, mexido"):
            return "andamento"
        if first.startswith("conversa aberta"):
            return "aberta"
        if first.startswith("conversa"):
            return "conversa"
        if first.startswith("mexido"):
            return "recente"
        return "dentro" if "aberta dentro da pasta" in first else "uso"
    text = " ".join(w.blockers)
    for key, mark in (("merge", "merge não comprovado"), ("alterações", "alterações locais"),
                      ("ignorados", "ignorados que não voltam")):
        if mark in text:
            return key
    return "outros"


def n_worktrees(n: int) -> str:
    return f"{n} worktree" if n == 1 else f"{n} worktrees"


def indent(text: str | Text, by: int = 5) -> Padding:
    return Padding(text if isinstance(text, Text) else Text(text), (0, 0, 0, by))


def worktrees_summary(con: Console, rows: list[Worktree], root: Path | None, days: int,
                      stale_days: int = DEFAULTS["stale_worktree_days"]) -> None:
    def group(v: str) -> list[Worktree]:
        return [w for w in rows if w.verdict == v]
    def size(ws: list[Worktree]) -> str:
        return fmt(sum(w.size for w in ws))
    def breakdown(ws: list[Worktree]) -> list[str]:
        by: dict[str, list[Worktree]] = {}
        for w in ws:
            by.setdefault(worktree_category(w), []).append(w)
        return [indent(f"• {len(v)} {CATEGORIES[k][len(v) != 1].format(d=stale_days)} · {size(v)}")
                for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))]

    safe, later, keep = group("seguro"), group("depois"), group("manter")
    lines: list = [Text.assemble(("🟢 SEGURO PARA APAGAR  ", "bold green"),
                                 (f"{n_worktrees(len(safe))} · {size(safe)}", "bold"))]
    prazo = f", e parados há mais de {days} dias" if days else ""
    unmerged = [w for w in safe if not w.merged]
    merged = [w for w in safe if w.merged]
    if merged or not safe:
        who = f"{n_worktrees(len(merged))}: " if unmerged else ""
        lines.append(indent(Text(f"{who}Já estão na main (merge, squash ou rebase), sem alterações, sem arquivos que o "
                                 f"Git não recupera e sem nada rodando dentro da pasta{prazo}. A branch continua "
                                 "existindo.", style="green")))
    if unmerged:
        remotes = {w.pushed.partition("/")[0] for w in unmerged}
        checked = f"nas referências locais de {remotes.pop()}" if len(remotes) == 1 else "nas referências remotas locais"
        lines.append(indent(Text(f"{n_worktrees(len(unmerged))} sem merge, parados há mais de {stale_days} dias, com "
                                 f"tudo commitado e enviado (conferido {checked}): a pasta sai, a branch fica.",
                                 style="green")))
    reopen = [(w, link.conv) for w in safe for link in w.conversations if link.reopen]
    if reopen:
        lines.append(indent(Text("⚠ Reabra antes de apagar (a conversa começou dentro da pasta):", style="yellow")))
        for w, conv in reopen[:5]:
            lines.append(indent(Text(f"  {w.repo.name} / {where(w, root)}: {resume_command(conv)}", style="yellow")))
        if len(reopen) > 5:
            lines.append(indent(Text(f"  … mais {len(reopen) - 5}", style="yellow")))
    with_session: dict[str, int] = {}
    for w in safe:
        for name in w.sessions:
            with_session[name] = with_session.get(name, 0) + 1
    if with_session:
        names = ", ".join(f"{n} ({c})" for n, c in sorted(with_session.items(), key=lambda kv: -kv[1]))
        lines.append(indent(Text(f"Sessões do Claude Code abertas que trabalharam em alguns deles: {names}. Não "
                                 "impedem: o trabalho já está na main; a sessão só perde a pasta.", style="dim")))
    if later:
        lines += [Text(""), Text.assemble(("🟡 EM ANDAMENTO  ", "bold yellow"),
                                          (f"{n_worktrees(len(later))} · {size(later)}", "bold"),
                                          ("  não mexa: em uso, mexido ou conversado há pouco", "yellow"))]
        lines += breakdown(later)
    if keep:
        lines += [Text(""), Text.assemble(("⚪ MANTER  ", "bold"),
                                          (f"{n_worktrees(len(keep))} · {size(keep)}", "bold"), ("  não apague", "dim"))]
        lines += breakdown(keep)

    cmd = hogs_command(root, days) if root else "hogs --worktrees PASTA"
    todo = [Text(""), Text("O que fazer", style="bold")]
    step = 1
    if safe:
        todo.append(Text(f"  {step}. Apagar os 🟢: {cmd} --clean", style="bold"))
        todo.append(indent("Digite a para todos ou os números do quadro (ex: 1,3,5-7). Ele confirma antes e "
                           "confere tudo de novo antes de cada remoção."))
        step += 1
    if later:
        todo.append(Text(f"  {step}. 🟡 ficam: estão em uso ou em andamento (motivo na coluna Veredito). "
                         "Rode de novo depois."))
        step += 1
    if keep:
        todo.append(Text(f"  {step}. ⚪ ficam com você: o motivo de cada um está na coluna Veredito."))
    if not safe and not later:
        todo.append(Text("  Nada a apagar agora. Rode de novo daqui a alguns dias."))
    con.print(Panel(Group(*lines, *todo), title=f"Resumo · {n_worktrees(len(rows))} · {size(rows)}",
                    title_align="left", border_style="green" if safe else "yellow", padding=(1, 2)))
    con.print(Text("Merge e envio conferidos nas referências remotas locais; rode git fetch nos repositórios para "
                   "atualizá-las.", style="dim"))


def clean_worktrees(con: Console, rows: list[Worktree], days: int, root: Path | None = None) -> None:
    """Só os 🟢: todos de uma vez (a) ou pelos números do quadro, com confirmação e reverificação."""
    safe = safe_worktrees(rows)
    if not safe:
        con.print(Panel("Nenhum worktree 🟢 agora. O resumo acima diz o que falta para cada grupo.",
                        border_style="yellow", padding=(0, 2)))
        return
    t = Table(title="🗑  Worktrees seguros para apagar", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAD, expand=True)
    t.add_column("#", justify="right", style="cyan", no_wrap=True)
    t.add_column("Projeto", no_wrap=True)
    t.add_column("Worktree / branch", ratio=3)
    t.add_column("Tamanho", justify="right", no_wrap=True)
    t.add_column("Mexido", no_wrap=True)
    t.add_column("Na main", ratio=2)
    t.add_column("Sessão que trabalhou nele", ratio=2)
    for n, w in enumerate(safe, 1):
        t.add_row(str(n), w.repo.name, Text(where(w, root)).append(f"\n{branch_name(w)}", style="dim"),
                  fmt(w.size), elapsed(w.last_activity), merge_cell(w),
                  Text(", ".join(w.sessions) or "—", style="dim"))
    con.print(t)
    raw = Prompt.ask(f"Quais apagar? [cyan]a[/] = todos os {len(safe)} ({fmt(sum(w.size for w in safe))}) · "
                     "números, ex: [cyan]1,3,5-7[/] · Enter cancela", default="", show_default=False)
    numbers = selection_numbers(raw, len(safe))
    chosen = [w for n, w in enumerate(safe, 1) if n in numbers]
    if not chosen:
        con.print("[dim]Nada apagado.[/]")
        return

    con.print(Text(f"\nVou remover {n_worktrees(len(chosen))}, {fmt(sum(w.size for w in chosen))}:", style="bold"))
    for w in chosen:
        note = f"  · sessão {', '.join(w.sessions)}" if w.sessions else ""
        con.print(Text(f"  • {w.repo.name} / {where(w, root)}  ({branch_name(w)}){note}"))
        for link in w.conversations:
            if link.reopen:
                con.print(Text(f"    ⚠ reabra antes de apagar “{link.conv.title or link.conv.session[:8]}”: "
                               f"{resume_command(link.conv)}", style="yellow"))
    con.print(Text("As branches ficam. Antes de cada remoção confiro tudo de novo; se algo mudou, ele fica.",
                   style="dim"))
    if not Confirm.ask("Remover?", default=False):
        con.print("[dim]Nada apagado.[/]")
        return

    done, kept = [], []
    for w in chosen:
        try:
            with con.status(f"Removendo {where(w, root)}…"):
                remove_worktree(w, days, root)
        except (OSError, RuntimeError) as exc:
            kept.append((w, str(exc)))
            con.print(Text(f"  ↷ {where(w, root)}: mantido, {exc}", style="yellow"))
            continue
        done.append(w)
        con.print(Text(f"  ✔ {where(w, root)} removido", style="green"))
    msg = Text.assemble((f"✔ {n_worktrees(len(done))} removido{'s' if len(done) != 1 else ''}", "bold green"),
                        f" · {fmt(sum(w.size for w in done))} · livre agora: "
                        f"{fmt(shutil.disk_usage(DATA_VOLUME).free)}")
    if kept:
        msg.append(f"\n↷ {n_worktrees(len(kept))} mantido{'s' if len(kept) != 1 else ''} porque algo mudou "
                   "desde a análise (motivo acima)", style="yellow")
    if done:
        w = done[0]
        msg.append(f"\nPara recriar um: git -C {short(w.repo)} worktree add {short(w.path)} {branch_name(w)}",
                   style="dim")
    con.print(Panel(msg, border_style="green" if done else "yellow", padding=(0, 2)))


def selection_numbers(raw: str, total: int, everything: set[int] | None = None) -> set[int]:
    """'a' = `everything` (padrão: todos); '1,3,5-7' = esses. Números fora da lista são ignorados."""
    chosen: set[int] = set()
    for tok in re.split(r"[,\s]+", raw.strip().lower()):
        if tok == "a":
            chosen |= set(range(1, total + 1)) if everything is None else everything
        elif m := re.fullmatch(r"(\d+)-(\d+)", tok):
            # limitado à lista: "1-99999999999" não cria bilhões de números
            chosen |= set(range(max(1, int(m.group(1))), min(total, int(m.group(2))) + 1))
        elif tok.isdigit():
            chosen.add(int(tok))
    return {n for n in chosen if 1 <= n <= total}


def verdict_counts(rows: list[Worktree]) -> dict[str, tuple[int, int]]:
    """Quantos worktrees e quantos bytes em cada veredito, sempre com as três chaves."""
    counts = {v: (0, 0) for v in VERDICTS}
    for w in rows:
        n, size = counts[w.verdict]
        counts[w.verdict] = (n + 1, size + w.size)
    return counts


def worktrees_line(rows: list[Worktree], root: Path | None, error: str = "") -> Text:
    """O totalizador 🌳 do relatório principal."""
    if root is None:
        return Text("🌳 Worktrees: escolha a pasta dos seus projetos com hogs --worktrees PASTA (ela fica lembrada)",
                    style="dim")
    if error and not rows:
        return Text(f"🌳 Worktrees: não consegui conferir ({error})", style="yellow")
    if not rows:
        return Text(f"🌳 Worktrees: nenhum em {short(root)}")
    c = verdict_counts(rows)
    (a, sa), (b, _), (k, _) = c["seguro"], c["depois"], c["manter"]
    return Text.assemble(f"🌳 Worktrees: {len(rows)} · {fmt(sum(w.size for w in rows))} · "
                         f"{plural(a, 'pode sair', 'podem sair')} ({fmt(sa)})"
                         f" · {b} em andamento · {k} com trabalho só aqui",
                         (f"  detalhes: hogs --worktrees {short(root)}", "dim"))


def remove_worktrees(rows: list[Worktree], scope: Path) -> None:
    """Remove cada um reconferindo tudo; tenta todos e, se algum ficou, levanta com o primeiro motivo."""
    errors = []
    for w in rows:
        try:
            remove_worktree(w, 0, scope)
        except Exception as exc:   # o motivo vai para a mensagem; os outros ainda são tentados
            errors.append(f"{where(w, scope)}: {exc}")
    if errors:
        raise RuntimeError(f"{len(errors)} de {len(rows)} mantidos: {errors[0]}")


def worktree_item(rows: list[Worktree], root: Path) -> Item | None:
    """Os 🟢 viram um Item só no relatório principal e no --clean."""
    safe = safe_worktrees(rows)
    if not safe:
        return None
    merged = sum(1 for w in safe if w.merged)
    parts = ["a branch fica"]
    if merged:
        parts.append(f"{merged} na main")
    if len(safe) - merged:
        parts.append(plural(len(safe) - merged, "enviado e parado", "enviados e parados"))
    reopen = len({link.conv.session for w in safe for link in w.conversations if link.reopen})
    if reopen:
        parts.append(f"reabra {plural(reopen, 'conversa', 'conversas')} antes")
    return Item("worktree", f"Worktrees que podem sair ({len(safe)})", sum(w.size for w in safe), SAFE,
                lambda: remove_worktrees(safe, root), " · ".join(parts))


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
    return lambda: run_all([pd.action], timeout=900)


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


def big_cache_paths(sizes: dict[Path, int], mounts: list[Path]) -> list[Path]:
    """Filhos grandes de ~/Library/Caches fora de pastas montadas, do maior para o menor."""
    return [p for p, size in sorted(sizes.items(), key=lambda kv: -kv[1])
            if p.parent == CACHES and size >= BIG_ITEM and not under_mount(p, mounts)]


def cache_items(sizes: dict[Path, int], mounts: list[Path]) -> list[Item]:
    """Caches grandes em ~/Library/Caches que a lista não cobre; o app recria quando precisar."""
    return [Item("dev", f"Cache: {p.name}", int(sizes[p]), REDOWN, lambda p=p: shutil.rmtree(p),
                 "cache do app; ele recria quando precisar", path=p)
            for p in big_cache_paths(sizes, mounts)]


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
    info = plist_file(app / "Contents/Info.plist")
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


# ---------------------------------------------------------------- cópias de assinatura

def sign_clone_root() -> Path | None:
    r = sh(["getconf", "DARWIN_USER_TEMP_DIR"])
    if not r or r.returncode != 0 or not r.stdout.strip():
        return None
    root = Path(r.stdout.strip()).parent / "X"
    return root if root.is_dir() else None


@dataclass
class CloneGroup:
    app: str
    executable: str
    installed: str
    by_version: dict[str, list[Path]]


def gather_sign_clones(root: Path) -> list[CloneGroup]:
    groups: dict[str, CloneGroup] = {}
    try:
        clones = sorted(root.glob("*.code_sign_clone/code_sign_clone.*"))
    except OSError:
        return []
    for clone in clones:
        plist = next(iter(sorted(clone.glob("*/Contents/Info.plist"))), None)
        if plist is None:
            continue
        info = plist_file(plist)
        version = str(info.get("CFBundleShortVersionString") or "")
        bundle = plist.parent.parent.name
        app = bundle.removesuffix(".bundle").removesuffix(".app")
        if not version or not app:
            continue
        group = groups.get(app)
        if group is None:
            installed = ""
            for base in APP_GLOBS:
                installed = str(plist_file(base / f"{app}.app" / "Contents" / "Info.plist")
                                .get("CFBundleShortVersionString") or "")
                if installed:
                    break
            group = groups[app] = CloneGroup(app, str(info.get("CFBundleExecutable") or app), installed, {})
        group.by_version.setdefault(version, []).append(clone)
    return list(groups.values())


def sign_clone_items(groups: list[CloneGroup]) -> list[Item]:
    items = []
    for g in groups:
        if not g.installed:      # versão instalada desconhecida: na dúvida, não oferece nada
            continue
        old = {v: paths for v, paths in g.by_version.items() if v != g.installed}
        if not old:
            continue
        copies = [p for paths in old.values() for p in paths]
        size = sum(du(paths[0]) for paths in old.values())

        def action(copies=copies):
            for p in copies:
                shutil.rmtree(p)

        def guard(g=g):
            return f"feche o {g.app} antes" if process_running(g.executable) else None

        hint = f"versões {', '.join(sorted(old))}; a instalada é {g.installed}"
        items.append(Item("apps", f"Cópias antigas do {g.app} ({len(copies)})", size, SAFE, action, hint, guard,
                          path=copies[0]))
    return items


# ---------------------------------------------------------------- arquivos pessoais

PERSONAL_ROOTS = [HOME / "Desktop", HOME / "Documents", HOME / "Downloads", HOME / "Pictures"]
STALE_PERSONAL_DAYS = 180
SKIP_PACKAGES = (".photoslibrary", ".musiclibrary", ".app", ".tvlibrary", ".logicx", ".band", ".fcpbundle",
                 ".imovielibrary", ".vmwarevm", ".pvm", ".utm", ".bundle", ".pkg")


def spotlight_last_used(path: Path) -> float | None:
    r = sh(["mdls", "-name", "kMDItemLastUsedDate", "-raw", str(path)], timeout=10)
    if r is None or r.returncode != 0:
        return None
    return parse_epoch(r.stdout)


def stale_personal(roots: list[Path], mounts: list[Path], now: float) -> list[tuple[Path, int, float]]:
    """Arquivos grandes (bytes alocados) sem uso há muito tempo. Não segue links; pasta sem permissão é ignorada."""
    cutoff = now - STALE_PERSONAL_DAYS * 86400
    found: list[tuple[Path, int, float]] = []
    for root in roots:
        for dp, dirs, names in os.walk(root, followlinks=False, onerror=lambda e: None):
            here = Path(dp)
            if (here.name.lower().endswith(SKIP_PACKAGES) or (here / ".git").exists()
                    or under_mount(here, mounts, containing=False)):
                dirs[:] = []
                continue
            for name in names:
                p = here / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                size = st.st_blocks * 512
                recent = max(st.st_mtime, getattr(st, "st_birthtime", 0), st.st_ctime)
                if size < BIG_ITEM or recent >= cutoff or under_mount(p, mounts, containing=False):
                    continue
                last = max(recent, spotlight_last_used(p) or 0)
                if last < cutoff:
                    found.append((p, size, last))
    return found


def move_to_trash(path: Path) -> None:
    """Move para a Lixeira pelo Finder; o caminho vai como argv, nunca dentro do script."""
    require(sh(["osascript", "-e", "on run argv",
                "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
                "-e", "end run", str(path)]), "Finder")


def personal_items(found: list[tuple[Path, int, float]], now: float) -> list[Item]:
    items = []
    for p, size, last in found:
        meses = int((now - last) // (30 * 86400))
        items.append(Item("apps", f"Parado: {short(p)}", size, EVAL, lambda p=p: move_to_trash(p),
                          f"sem abrir há {meses} meses · vai para a Lixeira", kind="pessoal", path=p))
    return items


# ---------------------------------------------------------------- coleta

@dataclass
class Snapshot:
    total: int
    free: int
    docker: Docker
    items: list[Item]               # dev + apps
    others: list[tuple[str, int]]
    apps: list[AppInfo] = field(default_factory=list)
    system: SystemInfo | None = None
    advice: list[tuple[str, int, str]] = field(default_factory=list)   # só texto, nunca um Item
    mounts_known: bool = True
    projects: int = 0
    mounts: list[Path] = field(default_factory=list)
    worktrees: list[Worktree] = field(default_factory=list)     # todas as linhas, não só os 🟢
    worktrees_root: Path | None = None
    worktrees_error: str = ""       # a conferência falhou: o 🌳 diz isso em vez de "nenhum"


def projects_root() -> Path | None:
    """A pasta dos projetos (`worktrees_root` no config.json), se for uma pasta que existe."""
    root = load_config().get("worktrees_root")
    if not isinstance(root, str) or not root:
        return None
    path = Path(root).expanduser()
    return path if path.is_dir() else None


def clone_items() -> list[Item]:
    root = sign_clone_root()
    return sign_clone_items(gather_sign_clones(root)) if root else []


def gather(con: Console) -> Snapshot:
    known = {str(p) for pd in PATH_DEFS for p in pd.paths} | {str(DOCKER_CONTAINER)}
    def_paths = [p for pd in PATH_DEFS for p in pd.paths]
    candidates = scan_candidates(known)
    root = projects_root()
    paths = def_paths + candidates + ([root] if root and root not in candidates else [])
    now = time.time()

    sizes: dict[Path, int] = {}
    with con.status("[bold]🐷 Farejando o disco…[/]", spinner="dots") as status:
        with ThreadPoolExecutor(max_workers=8) as ex:
            fut_worktrees = ex.submit(gather_worktrees, root, 0) if root else None   # o mais demorado: primeiro
            fut_docker = ex.submit(gather_docker)
            fut_system = ex.submit(gather_system, now)
            fut_clones = ex.submit(clone_items)
            futs = {ex.submit(du, p): p for p in paths}
            for n, f in enumerate(as_completed(futs), 1):
                sizes[futs[f]] = f.result()
                status.update(f"[bold]🐷 Farejando o disco…[/] [dim]{n}/{len(paths)} pastas · Docker…[/]")
            status.update("[bold]🐷 Vendo quais apps você não abre há tempo…[/]")
            apps = gather_apps()
            status.update("[bold]🐷 Perguntando pro Docker…[/]")
            docker = fut_docker.result()
            try:   # regra OCI: antes de rollback_info e docker_clean_items lerem `docker.released`
                if root and oci_candidates(docker):   # só procura clones se alguma imagem tem as duas etiquetas
                    repos, _ = find_repos(root)
                    docker.released = released_images(docker, repos)
            except Exception:   # o relatório nunca cai por causa dela: os pontos de volta ficam pelo prazo
                docker.released = {}
            mounts, mounts_known = known_mounts(docker)
            personal: list[Item] = []
            if mounts_known:
                status.update("[bold]🐷 Procurando arquivos grandes parados…[/]")
                personal = personal_items(stale_personal(PERSONAL_ROOTS, mounts, now), now)
            system = fut_system.result()
            clones = fut_clones.result()
            worktrees: list[Worktree] = []
            worktrees_error = ""
            if fut_worktrees is not None:
                status.update("[bold]🌳 Conferindo worktrees…[/]")
                try:
                    worktrees, warnings = fut_worktrees.result()
                    if not worktrees and warnings:
                        worktrees_error = warnings[0]
                except Exception as exc:   # o relatório nunca cai por causa dos worktrees: diz que não conferiu
                    worktrees_error = str(exc) or type(exc).__name__

    items = [Item(pd.cat, pd.label, sum(sizes.get(p, 0) for p in pd.paths), pd.risk,
                  make_action(pd), pd.hint, make_guard(pd), path=pd.paths[0]) for pd in PATH_DEFS]
    shown: set[Path] = set()
    if mounts_known:
        cand_sizes = {p: sizes.get(p, 0) for p in candidates}
        shown = set(big_cache_paths(cand_sizes, mounts))
        items += cache_items(cand_sizes, mounts)
    items += clones + personal
    if root and (wt_item := worktree_item(worktrees, root)) is not None:
        items.append(wt_item)
    projects = sizes.get(root, 0) if root else 0

    def rest(p: Path) -> int:
        """Tamanho de `p` fora da pasta dos projetos, para não contar duas vezes na barra."""
        if root and inside(p, root):
            return 0
        return sizes.get(p, 0) - (projects if root and inside(root, p) else 0)
    # guarda a partir de 100 MB pro histórico; o relatório só mostra os de 1 GB+
    others = sorted(((short(p), rest(p)) for p in candidates if rest(p) >= 100e6 and p not in shown),
                    key=lambda x: -x[1])
    usage = shutil.disk_usage(DATA_VOLUME)
    return Snapshot(usage.total, usage.free, docker, items, others, apps, system,
                    system_advice(system) if system else [], mounts_known, projects, mounts, worktrees, root,
                    worktrees_error)


# ---------------------------------------------------------------- sistema

SWAP_HIGH = 4e9
UPTIME_LONG_DAYS = 14
PREBOOT_NORMAL = 6e9
UPDATE_SNAPSHOT = "com.apple.os.update-MSUPrepareUpdate"


@dataclass
class SystemInfo:
    volumes: dict[str, int]
    swap_used: int
    uptime_days: float
    update_pending: bool


def parse_apfs(data: bytes, container: str) -> dict[str, int]:
    """CapacityInUse por papel (Roles[0]) dos volumes do container; volumes sem papel ficam de fora."""
    info = plist_dict(data) or {}
    out: dict[str, int] = {}
    for c in info.get("Containers", []):
        if c.get("ContainerReference") != container:
            continue
        for v in c.get("Volumes", []):
            roles = v.get("Roles") or []
            if roles:
                out[roles[0]] = out.get(roles[0], 0) + int(v.get("CapacityInUse", 0))
    return out


def parse_swap(text: str) -> int:
    m = re.search(r"used\s*=\s*([\d.,]+)M", text)
    if not m:
        return 0
    try:
        return int(float(m.group(1).replace(",", ".")) * 2**20)
    except ValueError:
        return 0


def parse_boottime(text: str, now: float) -> float:
    m = re.search(r"sec\s*=\s*(\d+)", text)
    if not m:
        return 0.0
    return max(0.0, (now - int(m.group(1))) / 86400)


def gather_system(now: float) -> SystemInfo | None:
    info = plist_cmd(["diskutil", "info", "-plist", "/"])
    container = (info or {}).get("APFSContainerReference")
    if not container:
        return None
    r = sh(["diskutil", "apfs", "list", "-plist"])
    if not r or r.returncode != 0 or not r.stdout:
        return None
    volumes = parse_apfs(r.stdout.encode(), container)
    snaps = plist_cmd(["diskutil", "apfs", "listSnapshots", "-plist", "/"])
    if snaps is None:
        return None
    pending = any(s.get("SnapshotName") == UPDATE_SNAPSHOT for s in snaps.get("Snapshots", []))
    sw = sh(["sysctl", "-n", "vm.swapusage"])
    swap = parse_swap(sw.stdout) if sw and sw.returncode == 0 else 0
    bt = sh(["sysctl", "-n", "kern.boottime"])
    days = parse_boottime(bt.stdout, now) if bt and bt.returncode == 0 else 0.0
    return SystemInfo(volumes, swap, days, pending)


def system_advice(si: SystemInfo) -> list[tuple[str, int, str]]:
    """Só texto: (rótulo, estimativa em bytes, como fazer). Nunca executado, nunca um Item."""
    out: list[tuple[str, int, str]] = []
    if si.swap_used >= SWAP_HIGH and si.uptime_days >= UPTIME_LONG_DAYS:
        out.append(("Reiniciar o Mac", si.swap_used,
                    f"swap {fmt(si.swap_used)} · ligado há {int(si.uptime_days)} dias"))
    if si.update_pending:
        out.append(("Aplicar a atualização do macOS",
                    int(max(0, si.volumes.get("Preboot", 0) - PREBOOT_NORMAL)),
                    "Ajustes do Sistema › Geral › Atualização de Software"))
    return out


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


# Chaves do histórico para a pasta dos projetos: "Worktrees" é parte de "Projetos" (que fica com o resto).
HIST_WORKTREES = "Worktrees"
HIST_PROJECTS = "Projetos"


def snapshot_sizes(s: Snapshot) -> dict[str, int]:
    d = s.docker
    sizes = {"Docker.raw": d.raw_real}
    if d.running:
        sizes["Docker: imagens"] = sum(i["size"] for i in d.images)
        sizes["Docker: cache de build"] = d.build_cache + sum(b["size"] for b in d.builder_caches)
        sizes["Docker: volumes"] = sum(v["size"] for v in d.volumes)
    # Sem a contagem "(n)" no nome, pro histórico seguir o mesmo item; os parados para avaliar não são crescimento.
    # Os worktrees 🟢 já estão em HIST_WORKTREES, que é parte da pasta dos projetos.
    sizes.update({re.sub(r" \(\d+\)$", "", i.label): i.size for i in s.items
                  if i.risk is not EVAL and i.cat != "worktree"})
    worktrees = worktrees_size(s)
    if worktrees:
        sizes[HIST_WORKTREES] = worktrees
    if s.projects - worktrees:
        sizes[HIST_PROJECTS] = s.projects - worktrees
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


def comparable_sizes(current: dict[str, int], base: dict[str, int]) -> dict[str, int]:
    """Registros sem "Worktrees" têm os worktrees dentro de "Projetos": compara igual com igual. Sem nenhum dos
    dois no registro, a pasta dos projetos passou a ser medida agora, o que não é crescimento."""
    if HIST_WORKTREES in base:
        return current
    cur = dict(current)
    worktrees = cur.pop(HIST_WORKTREES, 0)
    if HIST_PROJECTS not in base:
        cur.pop(HIST_PROJECTS, None)
    elif HIST_PROJECTS in cur or worktrees:
        cur[HIST_PROJECTS] = cur.get(HIST_PROJECTS, 0) + worktrees
    return cur


def growth(current: dict[str, int], base: dict[str, int], min_delta: float = 100e6) -> list[tuple[str, int]]:
    """(item, quanto cresceu) desde `base`, comparando igual com igual (comparable_sizes)."""
    deltas = [(k, v - base.get(k, 0)) for k, v in comparable_sizes(current, base).items()]
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

    rollbacks = rollback_info(d)

    def group(pred):
        sel = [i for i in d.images if pred(i) and id(i) not in rollbacks]
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
        *rollback_rows(list(rollbacks.values())),
        (f"Imagens sem tag ({len(none)})", none_size, SAFE, "versões antigas órfãs"),
        ("Cache de build", d.build_cache, SAFE, "próximo build fica mais lento"),
        *[(f"Cache do builder {bc['builder']}", bc["size"], SAFE, "volume do BuildKit; o daemon.json não limita")
          for bc in d.builder_caches],
        *stopped_rows(d.stopped),
        (f"Volumes sem uso ({len(vol_free)})", sum(v["size"] for v in vol_free), INFO, "dados de bancos: revise à mão"),
        (f"Volumes em uso ({len(vol_used)})", sum(v["size"] for v in vol_used), INFO, "nunca mexo"),
    ]
    untracked = d.raw_real - d.total
    if untracked > 3 * GB:
        rows.append(("Não contabilizado pelo Docker", untracked, INFO, "VM e sobras; parte volta ao reiniciar o Docker"))
    return rows


def rollback_rows(rollbacks: list[Rollback]) -> list[tuple[str, int, Risk, str]]:
    """Cada ponto de volta numa linha própria, com o mesmo risco e a mesma dica do --clean."""
    return [(f"Ponto de volta {rb.img['name']}", rb.size, rb.risk, rb.hint) for rb in rollbacks]


def stopped_rows(stopped: list[dict]) -> list[tuple[str, int, Risk, str]]:
    """Containers parados com o mesmo risco do --clean: os antigos 🟢 e o resto pelo pior risco."""
    old = [c for c in stopped if is_old_stopped(c)]
    rest = [c for c in stopped if not is_old_stopped(c)]
    rows = []
    if old:
        rows.append((f"Containers parados há +{OLD_CONTAINER_DAYS} dias ({len(old)})",
                     sum(c["size"] for c in old), SAFE, ""))
    if rest:
        risk = max((stopped_risk(c) for c in rest), key=lambda r: r.rank)
        rows.append((f"Outros containers parados ({len(rest)})", sum(c["size"] for c in rest), risk,
                     "recentes ou de projeto rodando; veja um por um no --clean"))
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
    rollbacks = rollback_info(d)
    for i in imgs:
        if id(i) in rollbacks:
            risk, origin = rollbacks[id(i)].risk, "ponto de volta"
        else:
            risk, origin = (LOSS if i["origin"] == "local" else REDOWN), ORIGIN_LABEL[i["origin"]]
        t.add_row(i["name"], Text(fmt(i["size"]), style="bold"),
                  Text(f"{risk.icon} {origin}", style=risk.color), i["created"])
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


def worktrees_size(s: Snapshot) -> int:
    """Os worktrees moram dentro da pasta dos projetos: nunca contam mais do que ela."""
    return min(sum(w.size for w in s.worktrees), s.projects)


def breakdown(s: Snapshot) -> list[tuple[str, int, str]]:
    """O disco usado em partes (rótulo, bytes, cor), sem contar nada duas vezes: a soma menos "Livre" é o usado."""
    used = s.total - s.free
    volumes = s.system.volumes if s.system else {}
    macos = min(used, sum(v for k, v in volumes.items() if k != "Data"))
    # Os 🟡 para avaliar (arquivos parados) já estão dentro de pastas medidas: Downloads ou "Outras pastas".
    worktrees = worktrees_size(s)
    data = [("Docker", s.docker.raw_real, "dodger_blue1"),
            ("Worktrees", worktrees, "medium_purple1"),
            ("Caches de dev", sum(i.size for i in s.items if i.cat == "dev"), "magenta"),
            ("Apps e dados", sum(i.size for i in s.items if i.cat == "apps" and i.risk is not EVAL), "yellow"),
            ("Projetos", s.projects - worktrees, "cyan"),
            ("Outras pastas", sum(size for _, size in s.others), "dark_orange")]
    room, known = used - macos, sum(v for _, v, _ in data)
    if known > room:   # medidas que se sobrepõem (clones APFS, pastas aninhadas): encolhe só as do volume Data
        data = [(label, int(v * room / known), color) for label, v, color in data]
    unknown = max(0, room - sum(v for _, v, _ in data))
    return data[:5] + [("macOS e swap", macos, "steel_blue1"), data[5],
                       ("Não identificado", unknown, "grey50"), ("Livre", s.free, "green")]


TRIM_OFF_NOTE = "Docker não devolve espaço: TRIM desligado"


def docker_trim_off() -> bool:
    """TRIM do Docker Desktop desligado de forma explícita; sem a configuração, não se sabe (conta como ligado)."""
    return read_json(DOCKER_SETTINGS).get("DiskTRIM") is False


def trim_trapped(cat: str, trim_off: bool) -> bool:
    """Com o TRIM desligado, o que se apaga no Docker fica preso no Docker.raw: não volta ao macOS.
    O único critério do cabeçalho, do ✨ Recomendado agora e do "Dá pra liberar"."""
    return trim_off and cat == "docker"


def header(s: Snapshot, records: list[dict] | None = None, cleanable: list[Item] | None = None,
           trim_off: bool | None = None, compact: bool = False) -> Panel:
    used = s.total - s.free
    segs = breakdown(s)
    if cleanable is None:
        cleanable = clean_items(s)
    if trim_off is None:
        trim_off = docker_trim_off()
    recommended = s.free + sum(i.size for i in cleanable if i.risk is SAFE and not trim_trapped(i.cat, trim_off))

    width = 64
    bar, cum, pos = Text(), 0, 0
    for _, size, color in segs:
        cum += size
        end = round(cum / s.total * width)
        bar.append("█" * (end - pos), style=color)
        pos = end
    legend = Columns([Text.assemble(("■ ", color), f"{label} ", (fmt(size), "bold"))
                      for label, size, color in segs if size or label == "Livre"], padding=(0, 2))

    free_color = "red" if s.free < 30 * GB else "yellow" if s.free < 60 * GB else "green"
    line = Text.assemble(("Macintosh HD", "bold"), f"  ·  {fmt(used)} de {fmt(s.total)} usados  ·  ",
                         (f"{fmt(s.free)} livres", f"bold {free_color}"), "  ·  com o recomendado: ",
                         (f"{fmt(recommended)} livres", "bold green"))
    if trim_off:
        line.append(f" ({TRIM_OFF_NOTE})", style="yellow")
    body = [line, bar, legend] if compact else [line, Text(), bar, legend]
    if records and (t := trend(s, records, time.time())) is not None:
        body += [Text(), t]
    return Panel(Group(*body), title="[bold]🐷 hogs[/]  [dim]quem está comendo seu disco[/]",
                 title_align="left", border_style="grey50", padding=(0, 1) if compact else (1, 2))


def growth_table(s: Snapshot, records: list[dict]) -> Table | Text:
    now = time.time()
    base = baseline(records, now)
    if base is None:
        return Text("📈 O histórico começou agora: a partir de amanhã o hogs mostra o que mais cresceu. "
                    "Com o agente ligado (hogs --alert ou --docker-gc), ele também registra de hora em hora.",
                    style="dim")
    before = base.get("sizes", {})
    rows = growth(snapshot_sizes(s), before)[:8]
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
    for label, delta in rows:
        color = "red" if delta >= GB else "yellow"
        t.add_row(label, Text(fmt_delta(delta), style=f"bold {color}"), mini_bar(delta, top, color),
                  fmt(before.get(label, 0) + delta))
    return t


CLAUDE_KEEP_DAYS = 365
CLAUDE_DEFAULT_DAYS = 30


def claude_retention() -> tuple[bool, str, str] | None:
    """Quanto tempo o Claude Code guarda as conversas. Só lê; None se não há a pasta do Claude Code."""
    if not claude_dir().is_dir():
        return None
    settings = claude_dir() / "settings.json"
    days = positive_int(read_json(settings).get("cleanupPeriodDays"))
    if days is not None and days > CLAUDE_DEFAULT_DAYS:
        return True, f"Conversas do Claude Code guardadas por {days} dias", ""
    return (False, f"Conversas do Claude Code: apagadas depois de {days or CLAUDE_DEFAULT_DAYS} dias",
            f'para guardar mais, ponha "cleanupPeriodDays": {CLAUDE_KEEP_DAYS} em {short(settings)} (o hogs não altera)')


def prevention_panel(s: Snapshot, trim_off: bool | None = None) -> Panel | None:
    d = s.docker
    cfg = load_config()
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True)
    grid.add_column()

    def row(ok: bool | None, text: str, hint: str = "") -> None:
        icon = Text("✔", style="green") if ok else Text("ℹ", style="blue") if ok is None else Text("!", style="bold yellow")
        grid.add_row(icon, Text.assemble(text, (f"  {hint}" if hint else "", "dim")))

    if d.running or d.raw_real:
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

        if (docker_trim_off() if trim_off is None else trim_off):
            row(False, "TRIM desligado: o espaço apagado no Docker não volta pro macOS",
                "Docker › Settings › Resources › Advanced")
        if d.raw_limit:
            row(None, f"Teto do disco do Docker: {fmt(d.raw_limit)} ({d.raw_limit / s.total:.0%} do seu disco)",
                "é só um limite; reduzir apaga todas as imagens, containers e volumes")

    claude = claude_retention()
    if claude:
        row(*claude)
    if not grid.row_count:
        return None
    return Panel(grid, title="[bold]🛡  Prevenção[/]", title_align="left", border_style="grey50", padding=(0, 2))


CAT_TITLES = {"docker": "🐳 Docker", "dev": "🧰 Caches de dev", "apps": "📦 Apps e dados", "worktree": "🌳 Worktrees"}


def recommended_panel(s: Snapshot, cleanable: list[Item], trim_off: bool | None = None) -> Panel:
    """Só os 🟢 contam como recomendado; as ações do macOS (↻) e os 🟡 para avaliar aparecem à parte."""
    t = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False, pad_edge=False)
    t.add_column("O quê", ratio=4, overflow="fold")
    t.add_column("Libera", justify="right", no_wrap=True)
    t.add_column("Risco", no_wrap=True)
    t.add_column("Por quê", style="dim", ratio=3, overflow="fold")
    groups: dict[str, list[Item]] = {}
    for i in cleanable:
        if i.risk is SAFE:
            groups.setdefault(i.cat, []).append(i)
    if trim_off is None:
        trim_off = docker_trim_off()

    def freed(cat: str, items: list[Item]) -> int:
        return 0 if trim_trapped(cat, trim_off) else sum(i.size for i in items)
    for cat, items in sorted(groups.items(), key=lambda kv: -freed(*kv)):
        if trim_trapped(cat, trim_off):
            t.add_row(Text(CAT_TITLES.get(cat, cat), style="bold"), Text("—", style="dim"), "",
                      Text(TRIM_OFF_NOTE, style="yellow"))
        else:
            t.add_row(Text(CAT_TITLES.get(cat, cat), style="bold"), Text(fmt(freed(cat, items)), style="bold"), "", "")
        for i in sorted(items, key=lambda i: -i.size):
            why = i.guard() if i.guard else None   # ex.: app aberto; a guarda vale de novo na hora de apagar
            t.add_row(f"   {i.label}", Text(fmt(i.size), style="dim" if trim_trapped(cat, trim_off) else ""),
                      Text(SAFE.icon),
                      Text(why, style="yellow") if why else i.hint)
    for label, size, how in s.advice:
        t.add_row(f"💻 {label}", f"~{fmt(size)}" if size else "", Text("↻", style="cyan"), how)
    evals = [i for i in cleanable if i.risk is EVAL]
    files = [i for i in evals if i.kind == "pessoal"]
    rollbacks = [i for i in evals if i.kind == "ponto_de_volta"]
    names = ", ".join(i.label.removeprefix("Ponto de volta ") for i in rollbacks)
    for title, sel, why in (
            ("🗂  Arquivos parados para avaliar", files, "vão para a Lixeira"),
            ("🐳 Pontos de volta recentes", rollbacks, f"{RECENT_ROLLBACK}: {names}")):
        if sel:
            t.add_row(f"{title} ({len(sel)})", fmt(sum(i.size for i in sel)), Text(EVAL.icon),
                      f"{why} · um por um no hogs --clean")
    body: list = [t] if t.row_count else [Text("Nada recomendado agora. 🎉", style="green")]
    if not s.mounts_known:
        body.append(Text("Docker não respondeu: não sei quais pastas os containers usam, então não sugiro arquivos.",
                         style="yellow"))
    if s.advice:
        body.append(Text("↻ é só recomendação: o hogs nunca reinicia nem atualiza o Mac.", style="dim"))
    return Panel(Group(*body), title="[bold]✨ Recomendado agora[/]", title_align="left", border_style="green",
                 padding=(0, 2))


def protected_panel(s: Snapshot) -> Panel:
    d = s.docker
    lines = []
    if d.live:
        lines.append(f"{plural(len(d.live), 'container rodando', 'containers rodando')} ({preview(d.live)}) "
                     "e as imagens deles")
    if d.volumes:
        lines.append(f"{plural(len(d.volumes), 'volume', 'volumes')} do Docker "
                     f"({fmt(sum(v['size'] for v in d.volumes))}): dados de bancos, só você decide")
    if s.mounts:
        lines.append(f"{plural(len(s.mounts), 'caminho montado', 'caminhos montados')} em containers "
                     f"({preview([short(m) for m in s.mounts])}): nada ali dentro é sugerido")
    if not s.mounts_known:
        lines.append("Docker não respondeu: não sei o que está em uso.")
    counts = verdict_counts(s.worktrees)
    (b, _), (c, _) = counts["depois"], counts["manter"]
    busy = plural(b, "worktree em andamento", "worktrees em andamento")
    if b and c:
        lines.append(f"{busy} e {c} com trabalho só aqui: não mexo")
    elif b or c:
        lines.append(f"{busy if b else plural(c, 'worktree com trabalho só aqui', 'worktrees com trabalho só aqui')}"
                     ": não mexo")
    text = Text("\n".join(lines) if lines else "Nada em uso pelo Docker agora.")
    return Panel(text, title="[bold]🛡 Não mexo[/]", title_align="left", border_style="grey50", padding=(0, 2))


def report(con: Console, s: Snapshot, records: list[dict] | None = None) -> None:
    records = records or []
    cleanable = clean_items(s)
    trim_off = docker_trim_off()   # lido uma vez: cabeçalho, prevenção, recomendado e "Dá pra liberar" concordam
    con.print(header(s, records, cleanable, trim_off))
    con.print(worktrees_line(s.worktrees, s.worktrees_root, s.worktrees_error))
    con.print(recommended_panel(s, cleanable, trim_off))
    con.print(protected_panel(s))
    con.print(growth_table(s, records))
    con.print()
    def rows(cat: str) -> list[tuple[str, int, Risk, str]]:
        return [(i.label, i.size, i.risk, i.hint) for i in s.items if i.cat == cat]
    for t in (category_table(CAT_TITLES["docker"], docker_rows(s.docker)), prevention_panel(s, trim_off),
              unused_images_table(s.docker),
              category_table(CAT_TITLES["dev"], rows("dev")), category_table(CAT_TITLES["apps"], rows("apps")),
              category_table("🔎 Outros grandes (fora da lista acima)",
                             [(label, size, INFO, "") for label, size in s.others if size >= GB][:12])):
        if t is not None:
            con.print(t)
            con.print()
    if (t := apps_table(s.apps, 10)) is not None:
        con.print(t)
        con.print(Text("Lista maior: hogs --apps 20", style="dim"))
        con.print()

    freed = [i for i in cleanable if not trim_trapped(i.cat, trim_off)]
    safe = sum(i.size for i in freed if i.risk is SAFE)
    redown = sum(i.size for i in freed if i.risk is REDOWN)
    line = Text.assemble("Dá pra liberar  ", (f"🟢 {fmt(safe)} sem risco", "bold green"), "  ·  ",
                         (f"🟡 {fmt(redown)} que baixam de novo", "bold yellow"))
    if trim_off:
        line.append(f"  ({TRIM_OFF_NOTE})", style="yellow")
    line.append_text(Text.assemble("\n", ("Rode ", "dim"), ("hogs --clean", "bold cyan"),
                                   (" pra escolher o que apagar.", "dim")))
    con.print(Panel(line, border_style="green", padding=(0, 2)))


# ---------------------------------------------------------------- limpeza

def clean_items(s: Snapshot) -> list[Item]:
    items = docker_clean_items(s.docker) + [i for i in s.items if i.action]
    items = [i for i in items if i.size >= 1e6 or i.cat == "worktree"]
    return sorted(items, key=lambda i: (i.risk.rank, -i.size))


def parse_selection(raw: str, items: list[Item]) -> list[Item]:
    """Como selection_numbers, mas 'a' são só os 🟢."""
    safe = {n for n, i in enumerate(items, 1) if i.risk is SAFE}
    return [items[n - 1] for n in sorted(selection_numbers(raw, len(items), safe))]


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

    def report(i: Item, ok: bool, why: str) -> None:
        if ok:
            con.print(f"  [green]✔[/] {i.label}")
        else:
            con.print(Text(f"  ↷ {i.label}: {why}", style="yellow"))

    with con.status("Limpando…"):
        freed = apply_items(chosen, report)

    after = shutil.disk_usage(DATA_VOLUME).free
    msg = Text.assemble(("Liberado: ", "bold"), (fmt(freed), "bold green"),
                        f"  ·  livre agora: {fmt(after)}")
    if any(i.cat == "docker" for i in chosen):
        msg.append("\nO Docker devolve o espaço do Docker.raw aos poucos; se não mudar, reinicie o Docker Desktop.",
                   style="dim")
    con.print(Panel(msg, border_style="green", padding=(0, 2)))


# ---------------------------------------------------------------- tela: terminal


class Key(NamedTuple):
    name: str
    x: int = -1
    y: int = -1


SCREEN_ON = "\x1b[?1049h\x1b[?25l"
SCREEN_OFF = "\x1b[?25h\x1b[?1049l"
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

_CSI_FINAL_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left",
                   "H": "home", "F": "end", "Z": "backtab"}
_CSI_TILDE_KEYS = {"1": "home", "7": "home", "4": "end", "8": "end", "5": "pgup", "6": "pgdn"}
_CONTROL_KEYS = {0x0d: "enter", 0x0a: "enter", 0x20: "space", 0x09: "tab",
                 0x7f: "backspace", 0x08: "backspace", 0x03: "ctrl_c"}


def _csi_key(params: str, final: str) -> Key | None:
    """Tecla de uma sequência CSI completa; None quando não é uma que conhecemos."""
    if params.startswith("<") and final in "Mm":
        if final == "m":
            return None
        try:
            b, x, y = (int(p) for p in params[1:].split(";"))
        except ValueError:
            return None
        if b == 0:
            return Key("click", x - 1, y - 1)
        if b == 64:
            return Key("wheel_up", x - 1, y - 1)
        if b == 65:
            return Key("wheel_down", x - 1, y - 1)
        return None
    if final == "~":
        name = _CSI_TILDE_KEYS.get(params)
        return Key(name) if name else None
    name = _CSI_FINAL_KEYS.get(final)
    return Key(name) if name else None


def parse_input(data: bytes, final: bool = False) -> tuple[list[Key], bytes]:
    """Teclas e mouse (SGR) de bytes crus do terminal, mais o resto incompleto."""
    keys: list[Key] = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x1b:
            if i + 1 >= n:
                if final:
                    keys.append(Key("esc"))
                    i += 1
                break
            nxt = data[i + 1]
            if nxt == 0x5b:  # CSI
                j = i + 2
                while j < n and not 0x40 <= data[j] <= 0x7e:
                    j += 1
                if j >= n:
                    break
                key = _csi_key(data[i + 2:j].decode("latin-1"), chr(data[j]))
                if key:
                    keys.append(key)
                i = j + 1
            elif nxt == 0x4f:  # SS3
                if i + 2 >= n:
                    break
                name = _CSI_FINAL_KEYS.get(chr(data[i + 2]))
                if name:
                    keys.append(Key(name))
                i += 3
            else:
                keys.append(Key("esc"))
                i += 1
            continue
        if b in _CONTROL_KEYS:
            keys.append(Key(_CONTROL_KEYS[b]))
            i += 1
        elif b < 0x20:
            i += 1
        elif b < 0x80:
            keys.append(Key(chr(b)))
            i += 1
        else:
            size = 2 if b >= 0xc0 and b < 0xe0 else 3 if 0xe0 <= b < 0xf0 else 4 if 0xf0 <= b < 0xf8 else 1
            if i + size > n:
                break
            text = data[i:i + size].decode("utf-8", "ignore")
            if text:
                keys.append(Key(text))
            i += size
    return keys, (b"" if final else data[i:])


class Terminal:
    """Terminal em modo cru, tela alternativa e mouse; sempre restaurado ao sair."""

    def __init__(self, mouse: bool = True, fd: int | None = None, out: TextIO | None = None):
        self.mouse = mouse
        self.fd = sys.stdin.fileno() if fd is None else fd
        self.out = sys.stdout if out is None else out
        self._saved = None
        self._pending = b""

    def _start(self) -> None:
        self._saved = termios.tcgetattr(self.fd)
        raw = termios.tcgetattr(self.fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
        raw[0] &= ~(termios.IXON | termios.ICRNL)
        termios.tcsetattr(self.fd, termios.TCSANOW, raw)
        self.out.write(SCREEN_ON + (MOUSE_ON if self.mouse else ""))
        self.out.flush()

    def _stop(self) -> None:
        try:
            self.out.write((MOUSE_OFF if self.mouse else "") + SCREEN_OFF)
            self.out.flush()
        finally:
            if self._saved is not None:
                termios.tcsetattr(self.fd, termios.TCSAFLUSH, self._saved)

    def __enter__(self) -> "Terminal":
        self._start()
        return self

    def __exit__(self, *exc) -> bool:
        self._stop()
        return False

    def read(self, timeout: float) -> list[Key]:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            keys, self._pending = parse_input(self._pending, final=True) if self._pending == b"\x1b" else ([], self._pending)
            return keys
        self._pending += os.read(self.fd, 4096)
        keys, self._pending = parse_input(self._pending)
        if self._pending == b"\x1b":
            more, _, _ = select.select([self.fd], [], [], 0.03)
            if more:
                self._pending += os.read(self.fd, 4096)
                extra, self._pending = parse_input(self._pending)
                keys += extra
            else:
                extra, self._pending = parse_input(self._pending, final=True)
                keys += extra
        return keys

    def size(self) -> tuple[int, int]:
        s = shutil.get_terminal_size((80, 24))
        return s.columns, s.lines

    def draw(self, lines: list[str]) -> None:
        body = "".join(f"\x1b[{y + 1};1H{line}\x1b[K" for y, line in enumerate(lines))
        self.out.write("\x1b[?2026h" + body + "\x1b[?2026l")
        self.out.flush()

    @contextmanager
    def suspended(self) -> Iterator[None]:
        self._stop()
        try:
            yield
        finally:
            self._pending = b""
            self._start()


# ---------------------------------------------------------------- tela: lista


class Column(NamedTuple):
    title: str
    justify: str = "left"
    ratio: int | None = None
    width: int | None = None


@dataclass
class Row:
    key: str
    cells: list
    item: Item | None = None
    children: list[Row] = field(default_factory=list)
    limit: int = 0
    kind: str = "item"  # item | group | more | advice | info
    detail: list = field(default_factory=list)
    title: str = ""
    path: Path | None = None
    resume: Conversation | None = None
    how: str = ""
    group: str = ""


def item_key(i: Item) -> str:
    return f"{i.cat}|{i.label}"


class Visible(NamedTuple):
    row: Row
    depth: int
    parent: Row | None


@dataclass
class ListState:
    cursor: int = 0
    scroll: int = 0
    expanded: set[str] = field(default_factory=set)
    flt: str = ""


def _hidden_size(hidden: list[Row]) -> int:
    return sum(c.item.size for c in hidden if c.item is not None)


def visible_rows(rows: list[Row], expanded: set[str]) -> list[Visible]:
    out: list[Visible] = []

    def walk(row: Row, depth: int, parent: Row | None) -> None:
        out.append(Visible(row, depth, parent))
        if row.key not in expanded:
            return
        kids = row.children
        shown = kids
        if row.limit > 0 and len(kids) > row.limit and f"{row.key}/+" not in expanded:
            shown = kids[:row.limit]
        for c in shown:
            walk(c, depth + 1, row)
        if len(shown) < len(kids):
            hidden = kids[len(shown):]
            more = Row(key=f"{row.key}/+",
                       cells=[Text(f"… mais {len(hidden)}", style="dim"), fmt(_hidden_size(hidden))],
                       kind="more")
            out.append(Visible(more, depth + 1, row))

    for r in rows:
        walk(r, 0, None)
    return out


def checkable(row: Row) -> bool:
    return row.item is not None and row.item.action is not None and row.kind == "item"


def leaf_items(row: Row) -> list[Item]:
    out: list[Item] = []
    if checkable(row):
        out.append(row.item)
    for c in row.children:
        out.extend(leaf_items(c))
    return out


def mark_box(row: Row, marked: set[str]) -> str:
    leaves = leaf_items(row)
    if not leaves:
        return ""
    n = sum(1 for i in leaves if item_key(i) in marked)
    return "[x]" if n == len(leaves) else "[-]" if n else "[ ]"


def toggle(row: Row, marked: set[str]) -> None:
    keys = {item_key(i) for i in leaf_items(row)}
    if not keys:
        return
    if keys <= marked:
        marked -= keys
    else:
        marked |= keys


def _all_rows(rows: list[Row]) -> Iterator[Row]:
    for r in rows:
        yield r
        yield from _all_rows(r.children)


def mark_all_safe(rows: list[Row], marked: set[str]) -> int:
    n = 0
    for r in _all_rows(rows):
        if checkable(r) and r.item.risk is SAFE:
            k = item_key(r.item)
            if k not in marked:
                marked.add(k)
                n += 1
    return n


def collect_items(rows: list[Row]) -> dict[str, Item]:
    return {item_key(r.item): r.item for r in _all_rows(rows) if checkable(r)}


def move(state: ListState, total: int, delta: int, height: int) -> None:
    state.cursor = max(0, min(state.cursor + delta, total - 1)) if total > 0 else 0
    if state.cursor < state.scroll:
        state.scroll = state.cursor
    elif state.cursor >= state.scroll + height:
        state.scroll = state.cursor - height + 1
    state.scroll = max(0, min(state.scroll, max(0, total - height)))


def open_row(state: ListState, vis: list[Visible]) -> None:
    if not 0 <= state.cursor < len(vis):
        return
    v = vis[state.cursor]
    if v.row.kind == "more" and v.parent is not None:
        state.expanded.add(f"{v.parent.key}/+")
    elif v.row.children:
        if v.row.key not in state.expanded:
            state.expanded.add(v.row.key)
        elif state.cursor + 1 < len(vis):
            state.cursor += 1


def close_row(state: ListState, vis: list[Visible]) -> None:
    if not 0 <= state.cursor < len(vis):
        return
    v = vis[state.cursor]
    if v.row.children and v.row.key in state.expanded:
        state.expanded.discard(v.row.key)
    elif v.parent is not None:
        for n, o in enumerate(vis):
            if o.row is v.parent:
                state.cursor = n
                break


# ---------------------------------------------------------------- tela: itens e ações

RISK_WHY = {
    "seguro": "volta sozinho ou já está guardado em outro lugar",
    "baixa de novo": "volta baixando de novo",
    "perde algo": "não volta: só apague se tiver certeza",
    "para avaliar": "não volta sozinho: avalie você (arquivos vão para a Lixeira)",
    "só informação": "só informação: o hogs não apaga",
}


def worktree_items(rows: list[Worktree], root: Path, days: int = 0) -> list[Item]:
    """Um Item por worktree 🟢 (a tela apaga um por um; o relatório continua com `worktree_item`)."""
    items = []
    for w in safe_worktrees(rows):
        hint = f"já na main ({w.merged})" if w.merged else f"enviado ({w.pushed}), parado {elapsed(w.last_activity)}"
        if any(link.reopen for link in w.conversations):
            hint += " · ⚠ reabra antes de apagar"
        items.append(Item("worktree", f"{w.repo.name} / {where(w, root)}", w.size, SAFE,
                          lambda w=w: remove_worktree(w, days, root), hint, path=w.path, worktree=w))
    return items


def tui_items(s: Snapshot) -> list[Item]:
    items = [i for i in clean_items(s) if i.cat != "worktree"]
    if s.worktrees_root:
        items += worktree_items(s.worktrees, s.worktrees_root)
    return sorted(items, key=lambda i: (i.risk.rank, -i.size))


def apply_items(items: list[Item], notify: Callable[[Item, bool, str], None]) -> int:
    """Roda cada item reconferindo a guarda na hora; devolve os bytes livres a mais no volume de dados."""
    before = shutil.disk_usage(DATA_VOLUME).free
    for i in items:
        if i.guard and (why := i.guard()):
            notify(i, False, f"pulei, {why}")
            continue
        try:
            i.action()
        except (OSError, RuntimeError) as exc:
            notify(i, False, str(exc))
            continue
        notify(i, True, "")
    return max(0, shutil.disk_usage(DATA_VOLUME).free - before)


def worktree_detail(w: Worktree, root: Path | None) -> list[Text]:
    lines = [Text(f"{where(w, root)} · {branch_name(w)}", style="bold"),
             Text(f"{fmt(w.size)} · mexido {elapsed(w.last_activity)}")]
    for ok, text in w.checks:
        lines.append(Text(f"✓ {text}", style="green") if ok else Text(f"✗ {text}", style="yellow"))
    if w.users:
        lines.append(Text(f"▶ em uso por: {', '.join(w.users)}"))
    lines += [Text(f"🟡 {t}") for t in w.waits]
    lines += [Text(f"⚪ {t}") for t in w.blockers]
    icon, color = VERDICTS[w.verdict]
    verdict = f"{icon} {VERDICT_LABELS[w.verdict]}"
    if w.eligible:
        verdict += " · a pasta sai, a branch fica"
    lines.append(Text(verdict, style=color))
    for link in sorted(w.conversations, key=lambda l: -l.conv.last):
        c = link.conv
        lines.append(Text(f"💬 {'● ' if c.live else ''}“{c.title or c.session[:8]}” · última msg {elapsed(c.last)}"))
        lines.append(Text(resume_command(c), style="cyan"))
        if link.reopen:
            lines.append(Text("⚠ reabra antes de apagar: depois da remoção ela não abre mais", style="yellow"))
    return lines


def item_detail(i: Item, guard_text: str | None = None) -> list[Text]:
    lines = [Text(i.label, style="bold"),
             Text(f"{fmt(i.size)} · {i.risk.icon} {i.risk.label}: {RISK_WHY[i.risk.label]}")]
    if i.hint:
        lines.append(Text(i.hint))
    if guard_text:
        lines.append(Text(f"✗ {guard_text}", style="yellow"))
    if i.path:
        lines.append(Text(f"📁 {short(i.path)}", style=f"link file://{i.path}"))
    if i.worktree:
        lines += worktree_detail(i.worktree, None)
    return lines


# ---------------------------------------------------------------- tela: desenho


# ---------------------------------------------------------------- tela: abas Recomendado e Disco

REC_COLUMNS = [Column("O quê", ratio=4), Column("Libera", "right"), Column("Risco"), Column("Por quê", ratio=3)]
DISK_COLUMNS = [Column("O quê", ratio=4), Column("Tamanho", "right"), Column("Risco"), Column("Detalhe", ratio=3)]


def run_guards(items: list[Item]) -> dict[str, str | None]:
    """Roda a guarda de cada item que tem uma, uma vez só (ela pode chamar pgrep)."""
    return {item_key(i): i.guard() for i in items if i.guard}


def item_row(i: Item, guards: dict[str, str | None]) -> Row:
    guard = guards.get(item_key(i))
    resume = None
    if i.worktree and i.worktree.conversations:
        resume = max(i.worktree.conversations, key=lambda link: link.conv.last).conv
    return Row(item_key(i), [i.label, fmt(i.size), i.risk.icon, Text(guard, style="yellow") if guard else i.hint],
               i, detail=item_detail(i, guard), title=i.label, path=i.path, resume=resume, group=f"disk/{i.cat}")


def _group_row(key: str, title: str, total_cell: str, risk: str, note: str, kids: list[Row], detail: list,
               limit: int = 0) -> Row:
    return Row(key, [title, total_cell, risk, note], children=kids, limit=limit, kind="group", title=title,
               detail=detail)


def recommended_tree(s: Snapshot, items: list[Item], trim_off: bool,
                     guards: dict[str, str | None]) -> tuple[list[Column], list[Row]]:
    safe: dict[str, list[Item]] = {}
    for i in items:
        if i.risk is SAFE and i.action is not None:
            safe.setdefault(i.cat, []).append(i)
    groups: list[tuple[int, Row]] = []
    for cat, group in safe.items():
        group = sorted(group, key=lambda i: -i.size)
        total = sum(i.size for i in group)
        trapped = trim_trapped(cat, trim_off)
        title = f"🌳 Worktrees que podem sair ({len(group)})" if cat == "worktree" else CAT_TITLES[cat]
        detail = [Text(title, style="bold"),
                  Text(f"{plural(len(group), 'item', 'itens')} · {fmt(total)}")]
        if cat == "worktree":
            detail.append(Text("a pasta sai, a branch fica"))
        if trapped:
            detail.append(Text(TRIM_OFF_NOTE, style="yellow"))
        groups.append((total, _group_row(f"rec/{cat}", title, "—" if trapped else fmt(total), "🟢",
                                         TRIM_OFF_NOTE if trapped else "",
                                         [item_row(i, guards) for i in group], detail,
                                         limit=5 if cat == "worktree" else 0)))
    rows = [r for _, r in sorted(groups, key=lambda g: -g[0])]
    for n, (label, size, how) in enumerate(s.advice):
        rows.append(Row(f"rec/advice/{n}", [f"💻 {label}", f"~{fmt(size)}" if size else "",
                                            Text("↻", style="cyan"), how],
                        kind="advice", how=how, title=label,
                        detail=[label, how, "o hogs nunca reinicia nem atualiza o Mac"]))
    for kind, key, title, note in (("pessoal", "rec/eval/pessoal", "🗂  Arquivos parados para avaliar", "vão para a Lixeira"),
                                   ("ponto_de_volta", "rec/eval/ponto_de_volta", "🐳 Pontos de volta recentes",
                                    RECENT_ROLLBACK)):
        group = sorted((i for i in items if i.risk is EVAL and i.kind == kind), key=lambda i: -i.size)
        if group:
            total = sum(i.size for i in group)
            label = f"{title} ({len(group)})"
            rows.append(_group_row(key, label, fmt(total), "🟡", note, [item_row(i, guards) for i in group],
                                   [Text(label, style="bold"),
                                    Text(f"{plural(len(group), 'item', 'itens')} · {fmt(total)}"), Text(note)]))
    if not s.mounts_known:
        rows.append(Row("rec/note/mounts",
                        [Text("Docker não respondeu: não sei quais pastas os containers usam, "
                              "então não sugiro arquivos.", style="yellow")], kind="info"))
    return REC_COLUMNS, rows


def _info_row(key: str, cells: list) -> Row:
    return Row(key, cells, kind="info", title=str(cells[0]), detail=[str(c) for c in cells if str(c)])


def disk_tree(s: Snapshot, items: list[Item], trim_off: bool,
              guards: dict[str, str | None]) -> tuple[list[Column], list[Row]]:
    blocks = [("docker", CAT_TITLES["docker"]), ("dev", CAT_TITLES["dev"]), ("apps", CAT_TITLES["apps"]),
              ("worktree", CAT_TITLES["worktree"]), ("system", "💻 macOS e swap"), ("others", "🔎 Outros grandes")]
    rows: list[Row] = []
    for cat, title in blocks:
        kids = [item_row(i, guards) for i in sorted(items, key=lambda i: -i.size) if i.cat == cat]
        total = sum(c.item.size for c in kids)
        infos = [i for i in s.items if i.cat == cat and i.action is None and i.size >= MIN_SHOW]
        for i in sorted(infos, key=lambda i: -i.size):
            kids.append(_info_row(f"disk/info/{cat}/{i.label}", [i.label, fmt(i.size), "⚪", i.hint]))
            total += i.size
        if cat == "system":
            si = s.system
            if si:
                if si.swap_used:
                    kids.append(_info_row("disk/system/swap", ["Swap em uso", fmt(si.swap_used), "⚪", ""]))
                    total += si.swap_used
                for role, size in sorted(si.volumes.items(), key=lambda kv: -kv[1]):
                    kids.append(_info_row(f"disk/system/apfs/{role}", [f"Volume APFS: {role}", fmt(size), "⚪", ""]))
                    total += size
                kids.append(_info_row("disk/system/uptime", ["Tempo ligado", "", "⚪",
                                                              f"{int(si.uptime_days)} dias"]))
            for n, (label, size, how) in enumerate(s.advice):
                kids.append(Row(f"disk/advice/{n}", [f"💻 {label}", f"~{fmt(size)}" if size else "",
                                                     Text("↻", style="cyan"), how],
                                kind="advice", how=how, title=label,
                                detail=[label, how, "o hogs nunca reinicia nem atualiza o Mac"]))
        if cat == "others":
            for label, size in s.others:
                if size >= GB:
                    kids.append(_info_row(f"disk/others/{label}", [label, fmt(size), "⚪", ""]))
                    total += size
        if kids:
            rows.append(_group_row(f"disk/{cat}", title, fmt(total), "", plural(len(kids), "item", "itens"), kids,
                                   [Text(title, style="bold"), Text(f"{fmt(total)} · {plural(len(kids), 'item', 'itens')}")]))
    return DISK_COLUMNS, rows



# ---------------------------------------------------------------- tela: aba Worktrees


# ---------------------------------------------------------------- tela: aba Docker


# ---------------------------------------------------------------- tela: app


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


def positive_int(value: object) -> int | None:
    """`value` se for um inteiro maior que zero (bool não conta), senão None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def setting(key: str) -> object:
    """Valor do config.json se tiver o mesmo tipo do padrão (lista de str; int > 0), senão o padrão."""
    default = DEFAULTS[key]
    value = load_config().get(key)
    if isinstance(default, list):
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return value
    elif positive_int(value) is not None:
        return value
    return default


def remember_worktrees_root(path: Path) -> None:
    """Guarda a pasta do último --worktrees; o relatório mede ela como "Projetos"."""
    cfg = load_config()
    cfg["worktrees_root"] = str(path.expanduser().resolve())
    save_config(cfg)


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
    if not math.isfinite(value) or value <= 0:
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
    p = argparse.ArgumentParser(
        prog="hogs", description="Mostra quem está comendo seu disco e ajuda a limpar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exemplos:\n"
               "  hogs                          relatório do disco\n"
               "  hogs --clean                  escolhe o que apagar do relatório\n"
               "  hogs --worktrees ~/Projetos   worktrees Git: quais dá pra apagar e por quê\n"
               "  hogs --worktrees ~/Projetos --clean\n"
               "                                apaga os worktrees 🟢 (todos ou por número)")
    p.add_argument("--version", action="version", version=f"hogs {__version__}")
    p.add_argument("--clean", action="store_true",
                   help="escolhe o que apagar; com --worktrees, só os worktrees seguros (🟢)")
    p.add_argument("--worktrees", type=Path, metavar="PASTA",
                   help="acha os worktrees Git em PASTA e diz quais já estão na main e podem ser apagados")
    p.add_argument("--worktree-days", type=int, default=0, metavar="DIAS",
                   help="com --worktrees: também exige DIAS sem mudança (padrão: sem mínimo)")
    p.add_argument("--alert", nargs="?", const="status", metavar="GB|off|test",
                   help="aviso automático quando o espaço livre cair abaixo de GB")
    p.add_argument("--apps", nargs="?", const=20, type=int, metavar="N",
                   help="só os N apps menos usados (padrão 20)")
    p.add_argument("--docker-gc", nargs="?", const="status", metavar="GB|off",
                   help="mantém o cache de build de cada builder abaixo de GB (confere a cada 1h)")
    p.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.worktree_days < 0:
        p.error("--worktree-days não pode ser negativo")
    if args.worktrees and not args.worktrees.expanduser().is_dir():
        p.error("--worktrees precisa apontar para uma pasta existente")

    if args.agent:
        return agent_run()
    if args.alert:
        return alert(args.alert)
    if args.docker_gc:
        return docker_gc(args.docker_gc)
    if not HAS_RICH:
        sys.exit("hogs precisa da biblioteca rich:  pip install rich")

    con = Console()
    if args.worktrees:
        remember_worktrees_root(args.worktrees)
        with con.status("[bold]🌳 Conferindo worktrees, alterações e sinais de uso…[/]"):
            rows, warnings = gather_worktrees(args.worktrees, args.worktree_days)
        root = args.worktrees.expanduser().resolve()
        for warning in warnings:
            con.print(Text(warning, style="yellow"))
        worktrees_report(con, rows, root, args.worktree_days, setting("stale_worktree_days"))
        if args.clean:
            clean_worktrees(con, rows, args.worktree_days, root)
        return
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
