# Triagem Autônoma de Feedback do Usuário com Claude Code Headless

Demo pública da palestra do **BaIA** sobre como construir um pipeline de
triagem autônoma de feedback do usuário (bugs, melhorias, dúvidas) com
**Claude Code em modo headless** dentro do **GitHub Actions**.

Faz parte da **ShopFlow** (e-commerce fictício da org [`baia-demo`](https://github.com/baia-demo)):

| Repo | Stack | Função |
|---|---|---|
| [`storefront-web`](https://github.com/baia-demo/storefront-web) | Next.js 15 + Tailwind | UI da loja + widget "Central de ajuda" |
| [`catalog-api`](https://github.com/baia-demo/catalog-api) | Fastify + TS | Produtos / busca |
| [`orders-api`](https://github.com/baia-demo/orders-api) | Fastify + TS | Pedidos / total |
| [`user-feedback`](https://github.com/baia-demo/user-feedback) | (sem código) | Recebe issues dos relatos |
| **`apresentacao-baia`** (este) | Python + Actions | **Agente de triagem** |

> Em produção (Konsi) o trigger é uma Slack List; aqui usamos um form web
> que cria uma issue no GitHub. O padrão é o mesmo, mas mais portável.

## Fluxo

```
┌──────────────────────────────┐
│  ShopFlow (storefront-web)   │
│  Usuário clica                │
│  "Central de ajuda"          │
│  e preenche um form          │
└────────────┬─────────────────┘
             │ POST /api/feedback
             ▼
┌──────────────────────────────┐
│  Next.js API route           │
│  Cria issue em               │
│  baia-demo/user-feedback     │
│  com label "needs-triage"    │
└────────────┬─────────────────┘
             │ Workflow Relay
             │ (issues.labeled →
             │  repository_dispatch
             │  "feedback-labeled")
             ▼
┌──────────────────────────────┐
│  apresentacao-baia           │
│  triage.yml dispara          │
│  scripts/triage/triage.py    │
└────────────┬─────────────────┘
             │
             ▼
┌──────────────────────────────┐
│  Claude Code headless        │
│  - Clona catalog/orders/web  │
│  - Navega read-only          │
│    (Read/Glob/Grep/LS)       │
│  - Chama MCP tool            │
│    submit_triage(findings,   │
│                  user_reply) │
└────────────┬─────────────────┘
             │ findings[].kind:
             │  bug/improvement/
             │  question/unclear
             ▼
     ┌───────┴───────────────┐
     ▼                       ▼
┌──────────────┐    ┌──────────────┐
│ bug ou       │    │ question ou  │
│ improvement  │    │ unclear      │
│ (conf ≥ 0.5) │    │              │
│ → cria issue │    │ → só comenta │
│   no repo    │    │   na issue   │
│   técnico    │    │   original   │
│ + comenta +  │    │              │
│   fecha      │    │              │
└──────────────┘    └──────────────┘
```

## Decisões de design

- **Navegação autônoma > RAG sobre código.** O agente decide onde olhar a cada
  passo, sem snippets pré-selecionados. Pra arquiteturas pequenas/médias,
  busca por sintoma + Grep no repo certo encontra a causa mais rápido que um
  índice vetorial — e qualquer repo novo entra sem reindexar.
- **Read-only no agente.** `--allowedTools Read,Glob,Grep,LS`. Nunca edita.
- **Output estruturado via MCP custom tool.** Em vez de pedir JSON livre na
  resposta (frágil), o agente chama a tool `submit_triage` com schema
  Pydantic. Schema valida cada campo no momento da call.
- **Classificar antes de investigar.** Cada finding tem um `kind`
  (bug/improvement/question/unclear). Reduz "confirmation bias" (agente
  forçando como bug uma sugestão de melhoria).
- **Múltiplos findings por report.** Um relato pode mencionar 2+ pontos
  distintos — cada um vira issue técnica separada no repo certo.
- **Timeout duro.** `CLAUDE_TIMEOUT = 480s` + `--max-turns 25`. Cai num
  fallback "inconclusivo" se estourar.
- **Confiança < 0.5 ⇒ inconclusivo.** Não cria issue técnica, só rotula como
  `low-confidence` na issue original. Reduz ruído.
- **Idempotência via labels.** Remove `needs-triage` ANTES de processar
  (atomic). Primeiro runner a remover ganha — re-runs / dispatch concorrentes
  não duplicam.

## Estrutura

```
apresentacao-baia/
├── .github/workflows/
│   ├── triage.yml              # Roda aqui — chama triage.py
│   └── tests.yml               # CI dos testes unitários
└── scripts/triage/
    ├── triage.py               # Orchestrator (stdlib only)
    ├── triage_mcp_server.py    # MCP server com tool submit_triage
    ├── CLAUDE_TRIAGE.md        # Prompt do agente
    └── tests/                  # Testes unitários (unittest)
```

## Setup

### 1. Secrets do `apresentacao-baia`

| Secret | Descrição |
|---|---|
| `GH_PAT` | Fine-grained PAT com `issues:write` em `user-feedback` + repos-alvo |
| `ANTHROPIC_API_KEY` | API key da Anthropic |

### 2. Variables

| Variable | Exemplo | Descrição |
|---|---|---|
| `ORG_NAME` | `baia-demo` | Org dona dos repos |
| `REPORTS_REPO` | `baia-demo/user-feedback` | Repo onde os relatos viram issues |
| `TARGET_REPOS` | `catalog-api,orders-api,storefront-web` | Lista comma-separated dos repos analisados |

### 3. Secret + var no repo `user-feedback`

| Local | Nome | Valor |
|---|---|---|
| Secret | `RELAY_TOKEN` | PAT com `Contents:write` em `apresentacao-baia` (dispara `repository_dispatch`) |
| Variable | `TRIAGE_REPO` | `baia-demo/apresentacao-baia` |

A relay workflow já vive em `user-feedback/.github/workflows/relay.yml`.

### 4. Token na ShopFlow

No `storefront-web` (Fly secrets), defina `REPORTS_GITHUB_TOKEN` como um PAT
com `Issues:write` no `user-feedback`. O form web usa ele pra abrir as issues.

## Disparar

- **Automático:** novo relato no form → issue ganha `needs-triage` → relay
  dispara a triagem no `apresentacao-baia` via `feedback-labeled` event.
- **Manual em lote:** `gh workflow run triage.yml -f force_all=true` —
  processa todas as issues abertas com `needs-triage`.
- **Manual com limite:** `gh workflow run triage.yml -f max_issues=1`.

## Custos e métricas (medidos em runs reais)

- **Claude Code:** $0.05–0.30 por relato (variou conforme complexidade)
- **Turns típicos:** 4–7 com MCP tool (era 20–25 com JSON livre)
- **Latência:** ~30s do submit do form até issue técnica aparecer
- **Confiança típica em bugs reais:** 95–98%

## Versão de produção

A versão "real" desse pipeline roda na **Konsi** (fintech) sobre uma
arquitetura .NET multi-repo, usando uma **Slack List** como entrada em vez de
form web. O código é semelhante mas adaptado para os IDs específicos da
Slack API.

---

_Demo pública para a palestra do BaIA._
