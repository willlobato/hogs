<div align="center">

# 🐷 hogs

**Descubra quem está comendo o disco do seu Mac, inclusive o que o macOS esconde em “Dados do Sistema”, e limpe com segurança.**

[![CI](https://github.com/willlobato/hogs/actions/workflows/ci.yml/badge.svg)](https://github.com/willlobato/hogs/actions/workflows/ci.yml)
![macOS](https://img.shields.io/badge/macOS-12%2B-black?logo=apple)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

<img src="docs/report.svg" alt="Relatório do hogs no terminal" width="900">

</div>

---

## Por quê?

Num Mac de 500 GB, quem desenvolve vive com o disco no limite. Você abre **Ajustes › Armazenamento** e vê uma barra cinza gigante chamada **“Dados do Sistema”**, sem nenhuma pista do que tem lá dentro.

Quase sempre é o **Docker**. O Docker Desktop guarda tudo num único arquivo, o `Docker.raw`, que cresce em silêncio com imagens antigas, cache de build e containers esquecidos. Fácil passar de 100 GB. Somam-se a isso os caches de npm, Maven, Gradle, JetBrains, Playwright e Ollama, e mais uns apps que você não abre há anos.

O `hogs` responde em uns 20 segundos:

- **pra onde foi o espaço**, separado por categoria, numa barra parecida com a do macOS mas mostrando o Docker de verdade;
- **o que dá pra apagar sem medo**, com uma etiqueta de risco em cada item;
- **e apaga pra você**, só o que você escolher, nunca o que está em uso.

## Instalação

**Requisitos:** macOS e Python 3.9+ (o `python3` do sistema serve). O Docker é opcional: sem ele, o hogs mostra o resto normalmente.

### Opção 1: um comando (recomendado)

```bash
curl -fsSL https://raw.githubusercontent.com/willlobato/hogs/main/install.sh | bash
```

O script baixa o `hogs.py` para `~/.local/share/hogs`, instala a biblioteca [`rich`](https://github.com/Textualize/rich) se faltar e cria o comando `~/.local/bin/hogs`. Se `~/.local/bin` não estiver no seu `PATH`, ele mostra a linha pra colocar no `~/.zshrc`.

### Opção 2: pipx

```bash
pipx install git+https://github.com/willlobato/hogs.git
```

### Opção 3: manual

```bash
git clone https://github.com/willlobato/hogs.git ~/Development/hogs
python3 -m pip install --user rich
ln -s ~/Development/hogs/hogs.py ~/.local/bin/hogs
```

Confira com `hogs --version`.

## Uso

| Comando | O que faz |
|---|---|
| `hogs` | Relatório completo: disco, Docker, caches de dev, apps e o que mais estiver grande |
| `hogs --clean` | Lista o que dá pra apagar e apaga só o que você escolher |
| `hogs --apps [N]` | Os N apps que você menos usa (padrão: 20) |
| `hogs --alert 30` | Liga o aviso automático quando o espaço livre cair abaixo de 30 GB |
| `hogs --alert` | Mostra se o aviso está ligado |
| `hogs --alert off` | Desliga o aviso |
| `hogs --alert test` | Manda uma notificação de exemplo |

### O que o relatório mostra

- **🐳 Docker:** tamanho real do `Docker.raw` e o limite configurado. Também imagens em uso, imagens sem uso (separando as que voltam com `docker pull` das que foram construídas na sua máquina), imagens sem tag, cache de build, containers parados, volumes, e o espaço que a VM do Docker ocupa sem contabilizar.
- **🧰 Caches de dev:** npm, Yarn, pnpm, pip, Homebrew, Maven, Gradle, Go, Cargo, JetBrains, VS Code, Xcode, Playwright, Ollama, nvm, pyenv, SDKMAN e simuladores iOS.
- **📦 Apps e dados:** WhatsApp, Chrome, Teams, a VM do Claude (Cowork), backups do iPhone, Downloads, Lixeira e `/Applications`.
- **🔎 Outros grandes:** qualquer pasta acima de 1 GB na sua home ou em `~/Library` que não esteja nas listas acima. É aqui que aparecem as surpresas.
- **🗂 Apps menos usados:** o top 10, com tamanho do app e dos dados dele.

## Limpeza segura

Cada item tem uma etiqueta de risco:

| | Significa | Exemplos |
|---|---|---|
| 🟢 **seguro** | Se recria sozinho, você não perde nada | cache de build do Docker, npm, caches da JetBrains, Lixeira |
| 🟡 **baixa de novo** | Volta, mas precisa baixar ou reconstruir | imagens de registry, `~/.m2`, navegadores do Playwright |
| 🔴 **perde algo** | Não volta sozinho, só apague se tiver certeza | imagens Docker construídas localmente |
| ⚪ **só informação** | O hogs mostra, mas não apaga | volumes, WhatsApp, Downloads, apps |

```text
$ hogs --clean
 #  Item                                   Tamanho  Risco
 1  Docker: cache de build                 21,7 GB  🟢 seguro
 2  npm                                     4,2 GB  🟢 seguro
 3  Imagem registry.gitlab.com/acme/…       6,8 GB  🟡 baixa de novo
 …
Quais apagar? a = todos 🟢 · ex: 1,3,5-7 · Enter cancela: a
```

Você escolhe por número (`1,3,5-7`) ou digita `a` para todos os 🟢. Antes de apagar, o hogs mostra o resumo e pede confirmação. No fim, diz quanto liberou.

**Garantias:**

- ❌ Nunca toca em **containers rodando** nem em **imagens usadas por algum container**.
- ❌ Nunca apaga **volumes do Docker**, porque é lá que ficam os dados dos seus bancos.
- 🛡️ Um container parado de um projeto `docker compose` que ainda tem algo rodando nunca entra no grupo “seguro”.
- 🛡️ Imagens construídas localmente aparecem uma por uma, nunca em grupo.
- 🛡️ Não apaga a VM do Claude enquanto o app está aberto.
- Nada é apagado sem você confirmar.

## Aviso automático

```bash
hogs --alert 30     # liga: avisa quando ficar abaixo de 30 GB livres
hogs --alert off    # desliga
```

Um agente do `launchd` (o agendador do próprio macOS) roda `hogs --check` **a cada hora** e logo depois do login. A checagem é leve, leva menos de 1 segundo e só abre o Docker se for avisar. Abaixo do limite, aparece uma notificação como esta:

> **💾 Disco quase cheio: 18 GB livres**
> Docker ocupa 95 GB (~40 GB liberáveis). Rode hogs no terminal.

- No máximo **1 aviso a cada 6 horas**. Abaixo de 10 GB, vira **alerta crítico com som**, no máximo 1 por hora.
- Continua funcionando depois de reiniciar o Mac, até você rodar `--alert off`.
- O agente fica em `~/Library/LaunchAgents/dev.hogs.alert.plist` e o log em `~/Library/Logs/hogs.log`.

> [!TIP]
> Se a notificação de teste (`hogs --alert test`) não aparecer, libere as notificações do **Editor de Scripts** em *Ajustes do Sistema › Notificações*. O macOS envia notificações de script em nome dele.

## Apps menos usados

<img src="docs/apps.svg" alt="Apps menos usados" width="900">

```bash
hogs --apps        # top 20
hogs --apps 40     # quantos quiser
```

O hogs varre `/Applications` e `~/Applications` e ordena os apps pelo último uso. A coluna **+ Dados** soma o que o app deixa em `~/Library` (Containers, Application Support e Caches), espaço que continua ocupado se você só arrastar o app pro Lixo. O [AppCleaner](https://freemacsoft.net/appcleaner/) remove tudo junto.

## Como funciona

- **Tamanhos de pastas:** `du`, em paralelo. Os tamanhos estão em GB decimais, como no Finder.
- **Docker:** `docker system df -v`. Com o armazenamento containerd, a soma dos tamanhos “únicos” por imagem passa do total real, porque camadas compartilhadas entram mais de uma vez. Por isso o hogs ajusta as imagens proporcionalmente para que os totais batam com os do próprio Docker. O tamanho do `Docker.raw` é o espaço **realmente ocupado** no disco: o arquivo é esparso, então o tamanho nominal é só o limite.
- **De onde veio cada imagem:** o Docker não registra isso, então o hogs deduz pelo nome. Nome com endereço de registry (`ghcr.io/…`, `*.amazonaws.com/…`) ou com `usuario/` conta como baixável. Nome sem barra que não está na lista de imagens oficiais (`nginx`, `postgres`, …) conta como **construída aqui**. Na dúvida, o hogs marca como risco maior.
- **Último uso dos apps:** o hogs usa a data em que o executável do app foi lido pela última vez, que o macOS atualiza quando o app abre, combinada com a data do estado salvo da janela. Ele não depende do Spotlight, que às vezes está com o índice quebrado. A precisão é de dia, mas um antivírus que varre os executáveis pode “rejuvenescer” um app.

## Atualizar e desinstalar

**Atualizar:** rode o mesmo comando da instalação de novo, ou `pipx reinstall hogs`.

**Desinstalar:**

```bash
hogs --alert off                                   # remove o agente, se estiver ligado
rm -rf ~/.local/share/hogs ~/.local/bin/hogs       # instalação via script
pipx uninstall hogs                                # ou, se instalou com pipx
```

## Desenvolvimento

É um único arquivo, [`hogs.py`](hogs.py), e só depende do `rich`.

```bash
python3 -m unittest discover -s tests -v   # testes
python3 docs/make_screenshots.py           # regenera as imagens do README (dados fictícios)
```

Quer adicionar um cache ou app que o hogs ainda não conhece? É uma linha em `PATH_DEFS` dentro de `hogs.py`. PRs são bem-vindos.

## Licença

[MIT](LICENSE) © Will Lobato
