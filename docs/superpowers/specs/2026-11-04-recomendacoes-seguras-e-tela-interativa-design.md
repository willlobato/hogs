# hogs: recomendações seguras por bloco e tela interativa

Data: 2026-11-04 · Versão alvo: 0.4.0 · Autor do pedido: dono do projeto · Escrito a partir de uma sessão de desenho.

## Por que

No Mac do autor, o hogs mostra 466 de 494 GB usados e 28 GB livres. Desse total, **252,8 GB aparecem como
"Sistema e outros"**, um número que o hogs calcula por subtração (usado − Docker − caches de dev − apps) e não
explica. Uma investigação manual na mesma máquina mostrou o que havia ali:

| Parte | ~GB | Observação |
|---|---|---|
| `~/Development` | 56,7 | projetos e, sobretudo, worktrees Git |
| Volumes do sistema | 54,0 | macOS 18,0 · Preboot 17,3 (atualização do macOS preparada) · swap 16,1 · Recovery 2,6 |
| Pastas de sistema no volume Data | ~29 | /Library, /System, Homebrew (/opt), /private (db, tmp, sleepimage) |
| Apps fora da lista do hogs | ~25 | Claude 12,8, VS Code 6,5, JetBrains 2,9, Notion 2,4, ~/Applications 6,9 |
| Ferramentas e agentes | ~16 | .codex, .claude, .vscode, .local, caches de codex/copilot |
| Cópias do Chrome | ~8 (o `du` mostra 154) | 51 clones em `$TMPDIR/../X/com.google.Chrome.code_sign_clone`; 48 da versão instalada |
| Pessoais | ~9,5 | Pictures, Documents |
| Não identificado | ~30 | snapshots da atualização, metadados APFS, pastas sem permissão |

Achados que moldaram o desenho:

- O `du` superestima clones APFS: a soma por pasta deu 535,7 GB num volume de 411 GB. Tamanho mostrado como
  "dá para liberar" não pode vir do `du` quando há clones.
- Os worktrees são o maior gargalo do autor.
- Uma skill de QA do autor cria, a cada rodada, um ponto de volta `docker tag <img> <repo>:antes-<data-hora>`
  (ou `<repo>:<tag>-antes-<tema>`) e reconstrói a imagem; esses pontos acumulam (~1,4 GB cada na maior imagem).
  O QA vai do teste ao merge no mesmo dia, no máximo no dia seguinte.
- Os bancos de teste (`sqlserver`, `mariadb`) gravam os dados em pastas comuns montadas no container (uma dentro de
  `~/Development`, outra em `~/apps-config`). Uma regra de "arquivo parado há muito tempo" apontaria para elas.
- O Claude Code grava cada conversa com título (`aiTitle`), pasta (`cwd`), branch Git por mensagem (`gitBranch`) e
  datas; e apaga conversas com mais de 30 dias por padrão (`cleanupPeriodDays`).
- Swap com 13,8 GB em uso e o Mac ligado há 86 dias.

## Intenção

Que o hogs abra o "Sistema e outros" em blocos nomeados e, **em cada bloco, recomende só o que pode ser removido
sem que o autor perca algo importante ou tenha problema depois**. Quanto mais o hogs conseguir comprovar sozinho que
é seguro, melhor. "Importante", nas palavras do autor, é o que ele mexe com frequência. O que está parado há muito
tempo e não volta sozinho vira sugestão para ele avaliar, nunca remoção automática.

E que tudo isso seja usado numa tela interativa de terminal, rica e alinhada, mantendo o `rich` como única
dependência.

## Modelo de decisão (decidido)

Cada candidato é julgado por dois eixos:

1. **Usa?** Mexido ou usado recentemente (data de modificação, última abertura registrada pelo macOS, atividade Git,
   conversa do Claude Code recente, processo ou container usando agora).
2. **Se apagar, volta?** Verificado pelo hogs:
   - volta sozinho: cache, artefato de build regenerável, swap;
   - volta baixando: imagem de registry, app da App Store/Homebrew, dependências;
   - volta pelo Git: tudo commitado e enviado a um remoto;
   - volta pelo backup: coberto por backup recente do Time Machine (sinal opcional, ver "Deixado ao construtor");
   - não volta: o resto.

| | volta | não volta |
|---|---|---|
| não usa | 🟢 recomendado | 🟡 para avaliar |
| usa | 🟡 só se grande (cache que vai crescer de novo) | ⚪ não aparece como sugestão |

Tipos de ação: **apagar** (só o que volta), **mover para a Lixeira** (tudo o que é pessoal) e **ação recomendada**
sem apagar nada (reiniciar, aplicar atualização do macOS).

Como a matriz convive com os riscos que já existem (decisão do construtor, conservadora):
- O eixo "usa?" vale para conteúdo do autor (worktrees, arquivos pessoais, pontos de volta). Para o que **volta
  sozinho** (caches de build, caches de dev, artefatos regeneráveis), o uso não importa: continuam 🟢 como hoje.
- O que **volta baixando** continua 🟡 "baixa de novo" (imagens de registry sem uso, Maven etc.), como hoje.
- Os 🔴 atuais continuam: imagem construída aqui (uma por item) e container parado sem projeto. Aparecem só nas
  abas próprias (Docker), nunca em "Recomendado", nunca entram no `a`.
- **Nada vem marcado ao abrir.** `a` marca todos os 🟢 da lista visível, como o `a` do `--clean` de hoje. O esboço
  abaixo mostra a tela depois de um `a`.

## Regras por bloco (decididas)

### 🐳 Docker
- Pontos de volta de QA: tags `antes-*` e `*-antes-*`.
  - Se a imagem atual do mesmo repositório tem etiqueta OCI `org.opencontainers.image.revision` e esse commit já
    está na branch principal do repositório indicado por `org.opencontainers.image.source` (conferido no Git local,
    como nos worktrees), todos os `antes-*` mais antigos desse repositório são 🟢.
  - Sem etiqueta: `antes-*` com mais de **2 dias** é 🟢 (entra no "todos 🟢"); com até 2 dias é 🟡
    "ponto de volta recente, o QA pode estar em andamento".
  - Hoje nenhuma imagem do autor tem essas etiquetas; a regra dos 2 dias é a que vale na prática.
  - **Idade do ponto de volta.** O Docker não guarda quando uma tag foi criada, e o `Created` da imagem é a data do
    build antigo. A idade é a **mais recente** (o lado de maior risco) entre os sinais disponíveis:
    a data no nome (`antes-AAAAMMDD-HHMM`), o `Created` da imagem atual da mesma origem (`<repo>:latest` para
    `antes-*`; `<repo>:<tag>` para `<tag>-antes-*`), que é o rebuild feito logo depois da tag, e a primeira vez
    que o hogs viu essa tag (registrada no `DATA_DIR`). Sem nenhum sinal confiável: 🟡.
  - Só vale como ponto de volta se a imagem atual da mesma origem existe; sem ela, a imagem segue a regra de
    imagem construída aqui (🔴).
  - **Exceção explícita a um invariante atual** ("imagem construída aqui é sempre 🔴, uma por item"): decisão do
    autor, para quem o ponto de volta vira lixo depois do QA. Precisa de teste e de linha no `CLAUDE.md`. Na tela,
    cada ponto de volta aparece numa linha própria dentro do grupo, nunca escondido num total.
- Imagens `<none>` e caches de build continuam como hoje.
- Protegidos sempre: containers vivos (`ACTIVE_STATES`) e as imagens deles.
- **Nova proteção global:** qualquer pasta do host montada (bind mount) por qualquer container, rodando ou parado,
  nunca aparece como sugestão em nenhum bloco, nem nada dentro dela.

### 🌳 Worktrees
- **Totalizador no relatório principal**, por exemplo
  `🌳 Worktrees: 122 · 41,0 GB · 84 podem sair (23,5 GB) · 40 em andamento · 12 com alterações`.
  Os worktrees são procurados na pasta lembrada do último `--worktrees PASTA` (ver "Genérico por padrão").
- Vereditos:

| Situação | Veredito |
|---|---|
| Já na main (regra atual) | 🟢 |
| Sem merge, **+30 dias sem mexer**, tudo commitado **e enviado** (o HEAD está contido em alguma referência remota), nada rodando dentro, sem ignorado protegido | 🟢 "a pasta sai, a branch fica" |
| Sem merge e mexido nos últimos 30 dias, ou com conversa do Claude Code nos últimos 30 dias, ou com algo rodando dentro | 🟡 "em andamento, não mexa" |
| Alteração sem commit, commit não enviado, ignorado protegido (`.env` etc.) | ⚪ manter, qualquer idade |

- Todos os invariantes atuais de worktree continuam (remoção sem `--force`, branch preservada, reconferência
  imediata antes de remover).
- "Enviado" é conferido nas referências remotas locais, **sem rede** (o hogs nunca faz `fetch`). Elas podem estar
  velhas; por isso o texto é "enviado (conferido nas referências locais de origin)", com o aviso que já existe de
  rodar `git fetch`. A garantia real não depende disso: a remoção preserva a branch local com todos os commits.
- `--worktree-days N` mantém o sentido atual (mínimo extra de dias sem mexer para o 🟢 "já na main"). O prazo de
  30 dias do "parado sem merge" é fixo.
- **Conversas do Claude Code ligadas ao worktree**, inclusive as de sessões fechadas. Uma conversa está ligada se
  rodou dentro do worktree, se registrou a branch dele em `gitBranch` (no mesmo repositório) ou se citou o caminho
  dele. Para cada uma: título, data da última mensagem e comando para reabrir
  (`cd <cwd> && claude --resume <sessionId>`). Se a conversa rodou dentro do próprio worktree, avisar "reabra antes
  de apagar": depois da remoção ela não abre mais.
- Conversas são informação: não bloqueiam e não liberam nada sozinhas, exceto o critério "conversa nos últimos
  30 dias" do 🟡 acima.
- No painel 🛡 Prevenção: se `cleanupPeriodDays` não estiver configurado nas settings do Claude Code, recomendar
  aumentar (por exemplo 365), explicando que conversas de mais de 30 dias são apagadas. O hogs só recomenda; não
  altera as settings.

### 💻 macOS e swap
- Volumes do container APFS (`diskutil apfs list -plist`), swap (`sysctl vm.swapusage`), tempo ligado
  (`kern.boottime`) e atualização preparada (snapshot `com.apple.os.update-MSUPrepareUpdate` e Preboot acima do
  normal).
- Nunca apaga. Recomenda ↻ "reiniciar" quando o swap é alto e o Mac está ligado há muitos dias, e "aplicar a
  atualização do macOS" quando há uma preparada. A estimativa do que volta é conservadora.
- **O hogs nunca executa essas ações.** As linhas não têm caixa de marcar e não entram em "Marcados"; Enter nelas
  só mostra como fazer (onde clicar). Reiniciar com trabalho não salvo é decisão do autor.

### 🌐 Cópias de assinatura de apps (`*.code_sign_clone`)
- Pasta `X` irmã do `$TMPDIR` do usuário. Hoje: Chrome (51 cópias), JetBrains e Teams (vazias).
- 🟢 só as cópias cuja versão do bundle é diferente da versão instalada do app, e **só com o app fechado** (mesmo
  padrão de guarda da VM do Claude). As cópias da versão instalada aparecem como informação ("não liberam espaço:
  dividem os blocos com o app").
- O tamanho mostrado é o custo real, sem contar os blocos compartilhados.

### 📦 Apps fora da lista e 🧰 caches de ferramentas
- Apps (Claude, VS Code, Notion…): só informação, com a dica de onde limpar dentro do app.
- Caches em `~/Library/Caches/<app>`: 🟡 por item, só os grandes.

### 🗂 Pessoais
- Desktop, Documentos, Downloads, Imagens.
- Arquivos grandes sem abrir há mais de 6 meses viram 🟡 "para avaliar". A ação é **mover para a Lixeira**, nunca
  apagar direto.
- Nunca entram: bibliotecas de Fotos/Música (pacotes), pastas montadas por container e repositórios Git.

### Fora das sugestões nesta versão
- A pasta dos worktrees (no caso do autor, `~/Development`) fora dos worktrees: aparece na barra como "Projetos", sem sugestões. O autor disse que o gargalo
  são os worktrees, não a pasta inteira.

## Barra e relatório

- "Sistema e outros" deixa de existir como bloco opaco. A barra mostra partes nomeadas: Docker, Worktrees,
  Caches de dev, Apps e dados, Projetos, macOS e swap, Não identificado, Livre.
- Sem contagem dupla (worktrees dentro da pasta de projetos, clones APFS). A soma das partes nunca passa do usado;
  "Não identificado" é o resto, nunca negativo.
- O cabeçalho mostra "com o recomendado: X GB livres".

## Tela interativa (decidida)

Layout **"Empilhados"**, escolhido pelo autor entre modelos (a versão lado a lado foi descartada). Esboço com dados
fictícios; no terminal as colunas são tabelas do `rich`, alinhadas mesmo com emojis:

```text
╭─ 🐷 hogs · Macintosh HD ───────────────────────────────────────────────────────────╮
│ 466 de 494 GB usados · 28 GB livres · com o recomendado: 103 GB livres            │
│ ██████████████▓▓▓▓▓▓▓░░░░░▒▒▒▒▒▒▒▒▒▒▒▒▒▒████████████████▒▒▒▒▒▒░░░                  │
│ ■ Docker 92 · ■ Worktrees 41 · ■ Dev 18 · ■ Apps 86 · ■ macOS e swap 54 · ■ Livre 28│
╰───────────────────────────────────────────────────────────────────────────────────╯
╭─ ✨ Recomendado agora ─────────────────────────────────────────────────────────────╮
│     O quê                                   Libera  Risco  Por quê                │
│ [x] 🐳 Caches de build do Docker           40,4 GB   🟢    voltam no próximo build │
│ [x] ▾ 🌳 Worktrees que podem sair (84)     23,5 GB   🟢    a branch fica           │
│   [x] shop-api / checkout-v2                1,8 GB   🟢    já na main              │
│ ▶ [x] shop-api / relatorio-mensal           1,2 GB   🟢    enviado, parado há 41 d │
│   [x] … mais 82                            20,5 GB                                 │
│ [x] 🌐 Cópias antigas do Chrome             8,1 GB   🟢    feche o Chrome antes    │
│ [x] 🐳 Pontos de volta de QA (3)            2,9 GB   🟢    mais de 2 dias          │
│ [ ] 💻 Reiniciar o Mac                     ~20 GB    ↻     swap 13,8 GB · 86 dias  │
│ [ ] 🗂  Arquivos parados para avaliar (12) 16,1 GB   🟡    vão para a Lixeira      │
╰───────────────────────────────────────────────────────────────────────────────────╯
╭─ 🔎 Por que é seguro · shop-api / relatorio-mensal ───────────────────────────────╮
│ ✓ tudo commitado e enviado ao GitHub (branch feat/relatorio-mensal)               │
│ ✓ sem .env nem arquivo que o Git não recupera                                     │
│ ✓ nada rodando dentro, nenhum container                                           │
│ ✓ sem mexer desde 24/09 (41 dias)                                                 │
│ 💬 "Relatório mensal: agrupar por cliente" · última msg 24/09 · r reabrir         │
╰───────────────────────────────────────────────────────────────────────────────────╯
╭─ 🛡 Não mexo ──────────────────────────────────────────────────────────────────────╮
│ 40 worktrees em andamento · 12 com alterações · sqlserver e mariadb (dados montados)│
╰───────────────────────────────────────────────────────────────────────────────────╯
Marcados: 74,9 GB   ↑↓ mover  →← abrir  espaço marcar  r conversa  e explorar  Enter apagar
```

Painéis de cima para baixo, largura toda:

1. **🐷 hogs · Macintosh HD**: usado, livre, "com o recomendado", barra por partes e legenda.
2. **✨ Recomendado agora**: lista ordenada por mais espaço e menos risco, colunas alinhadas
   (marca, o quê, libera, risco, por quê). Cada linha abre ali mesmo nos seus itens, com caixinha de marcar.
   **Altura fixa com rolagem**, para que o painel de baixo fique sempre visível. Inclui as ações recomendadas
   (reiniciar, atualizar) e os itens 🟡 "para avaliar".
3. **🔎 Por que é seguro · <item em foco>**: texto completo do item (nada cortado), cada verificação feita com ✓/✗,
   conversas do Claude ligadas com o comando de reabrir.
4. **🛡 Não mexo**: o que ficou protegido e por quê, resumido.
5. Rodapé: total marcado e teclas.

### Abas (decididas)

O autor quer ver muito bem tudo o que toca o desenvolvimento dele; worktrees e Docker não podem ficar resumidos
numa linha. A tela tem abas, trocadas pelas teclas `1`–`4` ou Tab:

1. **✨ Recomendado** (abre aqui): o layout empilhado acima.
2. **🌳 Worktrees**: no topo, totais por veredito (🟢 podem sair · 🟡 em andamento · ⚪ com trabalho só aqui, com
   GB). Depois, um painel por projeto com **todos** os worktrees, não só os que podem sair. As colunas são as do
   `hogs --worktrees` de hoje (worktree/branch, tamanho, mexido, na main?, em uso por, veredito) mais **Conversa**
   (título da conversa do Claude Code ligada, com marca se a sessão está aberta). Embaixo, o painel de detalhe do
   worktree em foco: cada verificação com ✓/✗, o que roda dentro, commits não enviados, a conversa com
   reabrir/copiar e o caminho com "abrir no Finder". Filtro `f` por veredito; projetos longos mostram os primeiros e
   "… mais N", que abre com →.
3. **🐳 Docker**: no topo, Docker.raw e totais por risco (🟢 / 🟡 / 🔴 / ⚪ em uso). Painéis separados:
   - cache e sobras de build (caches por builder, imagens sem tag);
   - pontos de volta de QA, cada um em sua linha, com idade e situação (regra dos 2 dias);
   - todas as imagens, com origem, quem usa (container, ● se rodando) e onde ficam os dados montados do container
     que a usa (ex.: "dados em ~/…/mssql"); filtro `f` por em uso / sem uso / construídas aqui;
   - containers e volumes como informação protegida (nunca apagados).
4. **💾 Disco**: a exploração por bloco (a segunda tela, deixada ao construtor).

Referência visual (fora do repositório, dados reais): modelos `layout-v3-paineis.html` (Empilhados) e
`dev-views.html` na pasta `.superpowers/brainstorm/` da sessão de desenho, que é ignorada pelo Git.

Interação híbrida:
- Teclado é o principal: ↑↓ mover, →← abrir/fechar, espaço marcar, `a` marcar todos 🟢, `c` copiar o comando de
  reabrir a conversa ou o caminho do item, `o` abrir a pasta no Finder, `r` reabrir a conversa do Claude Code,
  `e` explorar o disco (segunda tela, por bloco), Enter apagar, `?` ajuda, `q` sair.
- Mouse: clique numa linha leva o foco até ela, clique na caixinha marca, rodinha rola. **Sem dica ao passar o
  mouse.** `--no-mouse` desliga. Seleção manual de texto no iTerm2 continua possível com Option+arrastar.
- Caminhos como links de terminal (Cmd+clique abre no Finder) se o iTerm2 mantiver isso com o mouse capturado;
  se não, a tecla `o` cobre.
- Enter abre a confirmação (lista do que sai, total, aviso se houver 🔴), depois reconfere cada item imediatamente
  antes de agir e mostra ✔/↷ por item, como o `--clean` de hoje.
- Fora de um terminal interativo (saída redirecionada, agente do launchd), o hogs imprime o relatório estático.
- Terminal do autor: iTerm2 3.7, truecolor.

### Linha de comando (decisão do construtor)

- `hogs` num terminal interativo abre a tela, na aba Recomendado. Fora de um terminal (saída redirecionada,
  agente), imprime o relatório estático, **novo**: os mesmos blocos nomeados, totalizador de worktrees e
  recomendações, sem interação.
- `hogs --report` força o relatório estático mesmo no terminal.
- `hogs --clean` continua existindo e abre a tela (é o mesmo que `hogs`); o fluxo numerado de hoje sai.
- `hogs --worktrees PASTA` abre a tela na aba Worktrees, limitada a PASTA; com `--report`, imprime o quadro.
  A pasta fica lembrada em `worktrees_root` no `config.json` para as próximas aberturas.
- `--alert`, `--docker-gc`, `--apps`, `--version` não mudam.
- `--no-mouse` desliga o mouse na tela.

## Genérico por padrão (decidido)

O hogs é público. As regras nasceram do fluxo do autor, mas nada pode depender dele:

- **Padrões de tag de ponto de volta** configuráveis (`rollback_tags` no `config.json`). O padrão já cobre nomes
  comuns em português e inglês, como `antes-*`, `*-antes-*`, `backup-*`, `*-backup-*`, `before-*`, `*-before-*`,
  `rollback-*` (a lista exata fica ao construtor). Uma tag só conta como ponto de volta se a imagem atual da mesma
  origem existe.
- **Prazos configuráveis:** `rollback_days` (padrão 2) e `stale_worktree_days` (padrão 30).
- **Pasta dos worktrees:** a última passada em `hogs --worktrees PASTA`, lembrada no `config.json`
  (`worktrees_root`). Sem nenhuma, a aba Worktrees explica como escolher a pasta; não há caminho fixo.
- **Agentes:** colunas e sinais do Claude Code e do Codex só aparecem se a ferramenta existir na máquina.
- **Terminal:** mouse, links e cores degradam sem quebrar em terminais que não suportam (o autor usa iTerm2).
- Exemplos, testes e screenshots usam nomes fictícios.

## Metas

1. Explicar o "Sistema e outros" em partes nomeadas e medidas de verdade.
2. Recomendar por bloco só o que é comprovadamente seguro, com o porquê visível.
3. Tornar os worktrees parados sem merge recuperáveis sem risco.
4. Limpar os pontos de volta de QA automaticamente pelo tempo (e pelo commit, quando houver etiqueta).
5. Tela interativa empilhada, alinhada, com teclado e mouse.

## Não metas

- Dependência nova (Textual ou outra). Só `rich` e a biblioteca padrão.
- Dica ao passar o mouse; layout lado a lado.
- Cruzar nome de imagem ou tag com PRs do GitHub, ou usar a API do GitHub.
- Mudar os scripts de build dos projetos do autor para gravar as etiquetas OCI (tarefa opcional e separada, em outros
  repositórios).
- Sugerir projetos inteiros da pasta dos worktrees para remoção.
- Apagar volumes do sistema, swap ou snapshots diretamente.
- Alterar settings do Claude Code automaticamente.

## Anti-metas (seria fracasso mesmo funcionando)

- Qualquer 🟢 que, apagado, faça o autor perder algo que ele queria: dado de banco de teste, `.env`, trabalho não
  enviado, arquivo pessoal.
- Mostrar como "dá para liberar" um tamanho que não volta de verdade (como os 154 GB dos clones do Chrome).
- Texto cortado sem jeito de ler inteiro; colunas desalinhadas.
- Arquivo pessoal apagado sem passar pela Lixeira.

## Restrições do projeto

- Um arquivo só (`hogs.py`); `install.sh` e `pyproject.toml` assumem isso. Python 3.9 (CI em 3.9 e 3.12).
  macOS apenas.
- Texto da interface em pt-BR, tamanhos com vírgula (`fmt()`), flags em inglês.
- Todo invariante novo de segurança ganha teste em `tests/test_hogs.py` e linha no `CLAUDE.md`.
- Screenshots só por `docs/make_screenshots.py`, com dados fictícios; a tela interativa entra lá como quadro
  renderizado.
- README atualizado (uso, garantias). Versão em `hogs.py` e `pyproject.toml` juntas.

## Deixado ao construtor

- Organização interna do código, nomes e como dividir em fases/PRs (sugestão: motor de blocos e regras primeiro,
  depois worktrees e conversas, depois a tela).
- Como medir o custo real de clones APFS (por exemplo, tamanho privado via `getattrlist`) e como obter "última
  abertura" de um arquivo.
- Limiares não fixados acima: "swap alto", "muitos dias ligado", "Preboot acima do normal", "grande" para caches e
  arquivos pessoais.
- Se o Time Machine entra como sinal de "volta pelo backup" nesta versão.
- Desenho da segunda tela (`e`, explorar disco).
- Desempenho da varredura e exibição de progresso enquanto mede.
