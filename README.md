# Triagem Autônoma de Bugs com Claude Code Headless

Demo pública da palestra do **BaIA** sobre como construir um pipeline de
triagem autônoma de bugs com **Claude Code em modo headless** dentro do
**GitHub Actions**.

Faz parte da **ShopFlow** (e-commerce fictício da org [`baia-demo`](https://github.com/baia-demo)):

| Repo | Stack | Função |
|---|---|---|
| [`storefront-web`](https://github.com/baia-demo/storefront-web) | Next.js 15 + Tailwind | UI da loja + form de "Reportar bug" |
| [`catalog-api`](https://github.com/baia-demo/catalog-api) | Fastify + TS | Produtos / busca |
| [`orders-api`](https://github.com/baia-demo/orders-api) | Fastify + TS | Pedidos / total |
| [`bug-reports`](https://github.com/baia-demo/bug-reports) | (sem código) | Recebe issues dos reports |
| **`apresentacao-baia`** (este) | Python + Actions | **Agente de triagem** |

> Em produção (Konsi) o trigger é uma Slack List; aqui usamos um form web
> que cria uma issue no GitHub. O padrão é o mesmo, mas mais portável.

## Fluxo

```
┌──────────────────────────────┐
│  ShopFlow (storefront-web)   │
│  Usuário clica "Reportar bug"│
│  e preenche um form          │
└────────────┬─────────────────┘
             │ POST /api/report-bug
             ▼
┌──────────────────────────────┐
│  Next.js API route           │
│  Cria issue em               │
│  baia-demo/bug-reports       │
│  com label "needs-triage"    │
└────────────┬─────────────────┘
             │ Workflow Relay
             │ (issues.labeled →
             │  repository_dispatch)
             ▼
┌──────────────────────────────┐
│  apresentacao-baia           │
│  bug-triage.yml dispara      │
│  triage.py                   │
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│  Claude Code headless        │
│  - Clona catalog/orders/web  │
│  - Navega read-only          │
│    (Read/Glob/Grep/LS)       │
│  - Retorna JSON estruturado  │
└────────────┬─────────────────┘
             │
     ┌───────┴───────┐
     ▼               ▼
┌──────────┐  ┌──────────────┐
│ É bug?   │  │ Não é bug?   │
│ → cria   │  │ → comenta    │
│   issue  │  │   na issue   │
│   no repo│  │   original   │
│   certo  │  └──────────────┘
│ + comen- │
│   ta +   │
│   fecha  │
└──────────┘
```

## Decisões de design

- **Navegação autônoma > RAG sobre código.** O agente decide onde olhar a cada
  passo, sem snippets pré-selecionados. Pra arquiteturas pequenas/médias,
  busca por sintoma + Grep no repo certo encontra a causa mais rápido que um
  índice vetorial — e qualquer repo novo entra sem reindexar.
- **Read-only no agente.** `--allowedTools Read,Glob,Grep,LS`. Nunca edita.
- **Timeout duro.** `CLAUDE_TIMEOUT = 480s` + `--max-turns 25`. Cai num
  fallback "inconclusivo" se estourar.
- **Confiança < 0.5 ⇒ inconclusivo.** Não cria issue técnica, só rotula como
  `low-confidence` na issue original. Reduz ruído.
- **JSON-first.** Quando o modelo gasta os turnos sem retornar JSON, usa
  `--resume` pra pedir só o JSON final.
- **Idempotência via labels.** A issue original ganha `triaged` e perde
  `needs-triage`, então re-runs do mesmo evento não duplicam triagem.

## Estrutura

```
apresentacao-baia/
├── .github/workflows/
│   ├── bug-triage.yml          # Roda aqui — chama o triage.py
│   └── relay-bug-reports.yml   # Referência — copie pro repo bug-reports
└── scripts/bug-triage/
    ├── triage.py               # Script principal (stdlib only)
    └── CLAUDE_TRIAGE.md        # Prompt do agente
```

## Setup

### 1. Secrets do `apresentacao-baia`

| Secret | Descrição |
|---|---|
| `GH_PAT` | Fine-grained PAT com `issues:write` em `bug-reports` + repos-alvo |
| `ANTHROPIC_API_KEY` | API key da Anthropic |

### 2. Variables

| Variable | Exemplo | Descrição |
|---|---|---|
| `GITHUB_ORG` | `baia-demo` | Org dona dos repos |
| `REPORTS_REPO` | `baia-demo/bug-reports` | Repo onde os reports viram issues |
| `TARGET_REPOS` | `catalog-api,orders-api,storefront-web` | Lista comma-separated dos repos analisados |

### 3. Secret + var no repo `bug-reports`

| Local | Nome | Valor |
|---|---|---|
| Secret | `RELAY_TOKEN` | PAT com `repo` (dispara `repository_dispatch` no apresentacao-baia) |
| Variable | `TRIAGE_REPO` | `baia-demo/apresentacao-baia` |

E copie `.github/workflows/relay-bug-reports.yml` deste repo pra dentro do
`bug-reports` (caminho `.github/workflows/relay.yml`).

### 4. Token na ShopFlow

No `storefront-web` (Fly secrets), defina `REPORTS_GITHUB_TOKEN` como um PAT
com `issues:write` no `bug-reports`. O form web usa ele pra abrir as issues.

## Disparar

- **Automático:** novo report no form → issue ganha `needs-triage` → relay
  dispara o triage no `apresentacao-baia`.
- **Manual em lote:** `gh workflow run bug-triage.yml -f force_all=true` —
  processa todas as issues abertas com `needs-triage`.
- **Manual com limite:** `gh workflow run bug-triage.yml -f max_issues=1`.

## Custos típicos

- **Claude Code:** ~$0.05–0.30 por bug
- **GitHub Actions:** 2–5 min por bug em `ubuntu-latest`
- **Latência:** ~30s do report até a issue técnica aparecer no repo-alvo
  (quase tudo é boot do runner + npm install do CLI)

## Versão de produção

A versão "real" desse pipeline roda na **Konsi** (fintech) sobre uma
arquitetura .NET multi-repo, usando uma **Slack List** como entrada em vez de
form web. O código é semelhante mas adaptado para os IDs específicos da
Slack API.

---

_Demo pública para a palestra do BaIA._
