"""
Bug Triage Automático via GitHub Issues + Claude Code Headless + MCP tool.

Demo da palestra "Triagem Autônoma de Bugs com Claude Code Headless" (BaIA).

O usuário reporta um bug pelo form web da ShopFlow, que abre uma issue em
`baia-demo/bug-reports` com label `needs-triage`. Este script é disparado
pelo GitHub Actions e:

  1. Lê o título e corpo da issue
  2. Remove a label `needs-triage` atomicamente (idempotência)
  3. Clona os repositórios da arquitetura (shallow)
  4. Dispara Claude Code em modo headless (read-only), conectado a um
     MCP server local que expõe a tool `submit_triage`
  5. Lê o veredito do arquivo JSON gravado pelo MCP server (output estruturado
     com validação de schema dentro da tool — não dependemos de JSON livre)
  6. Se for bug com confiança >= 0.5: cria issue no repo correto + comenta + fecha
  7. Senão: comenta na issue original + atualiza labels
"""

import json
import logging
import os
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GH_PAT = os.environ["GH_PAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_ORG = os.environ["GITHUB_ORG"]
REPORTS_REPO = os.environ["REPORTS_REPO"]
TARGET_REPOS = [
    r.strip()
    for r in os.environ["TARGET_REPOS"].split(",")
    if r.strip()
]

TRIAGE_LABEL = os.environ.get("TRIAGE_LABEL", "needs-triage")
TRIAGED_LABEL = os.environ.get("TRIAGED_LABEL", "triaged")
IS_BUG_LABEL = os.environ.get("IS_BUG_LABEL", "is-bug")
NOT_BUG_LABEL = os.environ.get("NOT_BUG_LABEL", "not-a-bug")
TRIGGERING_ISSUE = (
    int(os.environ["TRIGGERING_ISSUE"])
    if os.environ.get("TRIGGERING_ISSUE")
    else None
)
FORCE_ALL = os.environ.get("FORCE_ALL", "false").lower() == "true"
MAX_ISSUES = int(os.environ.get("MAX_ISSUES", "0"))

SCRIPT_DIR = Path(__file__).parent
CLAUDE_TRIAGE_MD = SCRIPT_DIR / "CLAUDE_TRIAGE.md"
MCP_SERVER_SCRIPT = SCRIPT_DIR / "triage_mcp_server.py"
TRIAGE_OUTPUT_FILE = Path("/tmp/triage_result.json")
MCP_CONFIG_FILE = Path("/tmp/triage_mcp_config.json")
CLAUDE_TIMEOUT = 480

UNDEFINED_CONFIDENCE_THRESHOLD = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bug-triage")


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _github_request(
    method: str,
    path: str,
    body: dict | None = None,
    params: dict | None = None,
) -> dict | list | None:
    url = f"https://api.github.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def get_issue(repo: str, number: int) -> dict:
    return _github_request("GET", f"/repos/{repo}/issues/{number}")  # type: ignore[return-value]


def list_open_issues_with_label(repo: str, label: str) -> list[dict]:
    items = _github_request(
        "GET",
        f"/repos/{repo}/issues",
        params={"state": "open", "labels": label, "per_page": "100"},
    )
    return [i for i in items if "pull_request" not in i]  # type: ignore[union-attr]


def create_issue(repo: str, title: str, body: str, labels: list[str]) -> dict:
    return _github_request(
        "POST",
        f"/repos/{repo}/issues",
        body={"title": title, "body": body, "labels": labels},
    )  # type: ignore[return-value]


def comment_on_issue(repo: str, number: int, body: str) -> None:
    _github_request(
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        body={"body": body},
    )


def add_issue_labels(repo: str, number: int, labels: list[str]) -> None:
    _github_request(
        "POST",
        f"/repos/{repo}/issues/{number}/labels",
        body={"labels": labels},
    )


def remove_issue_label_atomic(repo: str, number: int, label: str) -> bool:
    """Remove uma label específica. Retorna False se já não estava lá.

    Usado pra idempotência — primeiro a remover ganha o processamento.
    """
    quoted = urllib.parse.quote(label, safe="")
    try:
        _github_request("DELETE", f"/repos/{repo}/issues/{number}/labels/{quoted}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def close_issue(repo: str, number: int) -> None:
    _github_request(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        body={"state": "closed", "state_reason": "completed"},
    )


# ---------------------------------------------------------------------------
# MCP config + Claude Code headless
# ---------------------------------------------------------------------------


def _write_mcp_config() -> Path:
    """Cria o arquivo de config MCP que o Claude Code lê pra spawnar o server."""
    config = {
        "mcpServers": {
            "triage": {
                "command": "python3",
                "args": [str(MCP_SERVER_SCRIPT)],
                "env": {
                    "VALID_REPOS": ",".join(TARGET_REPOS),
                    "TRIAGE_OUTPUT_FILE": str(TRIAGE_OUTPUT_FILE),
                },
            }
        }
    }
    MCP_CONFIG_FILE.write_text(json.dumps(config))
    return MCP_CONFIG_FILE


def run_claude_triage(title: str, body: str) -> dict:
    instructions = CLAUDE_TRIAGE_MD.read_text()

    prompt = f"""{instructions}

---

## Bug para análise

**Título:** {title}

**Descrição:**
{body}

---

Analise o bug acima navegando pelos repositórios em ./repos/. Quando tiver
o veredito, chame a tool `submit_triage` (UMA vez) e encerre.
"""

    # Limpa output anterior pra detectar se a tool foi chamada nesta run
    TRIAGE_OUTPUT_FILE.unlink(missing_ok=True)
    mcp_config = _write_mcp_config()

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--max-turns", "25",
                "--model", "claude-sonnet-4-6",
                "--mcp-config", str(mcp_config),
                "--allowedTools",
                "Read,Glob,Grep,LS,mcp__triage__submit_triage",
            ],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(Path.cwd()),
            env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        )
    except subprocess.TimeoutExpired:
        log.error("Claude Code timeout após %ds", CLAUDE_TIMEOUT)
        return _fallback_result(title, "Timeout na análise.")

    # Log de métricas
    try:
        outer = json.loads(result.stdout)
        log.info(
            "Claude Code: %d turns, %.1fs, $%.4f",
            outer.get("num_turns", 0),
            outer.get("duration_ms", 0) / 1000,
            outer.get("total_cost_usd", 0),
        )
    except (json.JSONDecodeError, TypeError):
        pass

    if result.returncode != 0:
        log.error("Claude Code rc=%d stderr: %s", result.returncode, result.stderr[:500])

    if not TRIAGE_OUTPUT_FILE.exists():
        log.warning(
            "submit_triage NÃO foi chamada. Claude stdout: %s",
            result.stdout[:500],
        )
        return _fallback_result(
            title,
            "Claude Code não chamou a tool submit_triage. "
            "Provavelmente estourou turns ou abandonou a análise.",
        )

    try:
        data = json.loads(TRIAGE_OUTPUT_FILE.read_text())
        log.info(
            "Veredito recebido via MCP: is_bug=%s confidence=%.2f target=%s",
            data.get("is_bug"),
            data.get("confidence", 0.0),
            data.get("target_repo"),
        )
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.error("Falha lendo output MCP: %s", e)
        return _fallback_result(title, f"Output MCP inválido: {e}")


def _fallback_result(title: str, reason: str) -> dict:
    return {
        "is_bug": False,
        "confidence": 0.0,
        "target_repo": None,
        "files_analyzed": [],
        "summary": f"Análise automática falhou para: {title}",
        "explanation": reason,
        "suggested_fix": None,
        "user_reply": (
            f"Não consegui analisar este bug automaticamente. Motivo: "
            f"{reason[:200]}. Vai precisar de uma olhada manual."
        ),
    }


# ---------------------------------------------------------------------------
# Processamento de uma issue
# ---------------------------------------------------------------------------


def process_issue(issue: dict) -> None:
    title = issue.get("title", "(sem título)")
    body = issue.get("body") or "(sem descrição)"
    number = issue["number"]

    # Idempotência: primeiro a remover a label ganha o processamento.
    # Se já foi removida (outro runner / re-trigger), abortamos sem efeitos.
    if not remove_issue_label_atomic(REPORTS_REPO, number, TRIAGE_LABEL):
        log.info(
            "Issue #%d: label %s já removida — outro runner processou. Skip.",
            number,
            TRIAGE_LABEL,
        )
        return

    log.info("=== Processando issue #%d: %s ===", number, title)

    result = run_claude_triage(title, body)

    confidence = float(result.get("confidence") or 0)
    is_bug = bool(result.get("is_bug"))
    target_repo = result.get("target_repo")

    target_issue_url: str | None = None
    if is_bug and target_repo and confidence >= UNDEFINED_CONFIDENCE_THRESHOLD:
        issue_body = _build_target_issue_body(issue, result)
        try:
            target_issue = create_issue(
                f"{GITHUB_ORG}/{target_repo}",
                f"[Auto-Triage] {title}",
                issue_body,
                labels=["bug", "auto-triage"],
            )
            target_issue_url = target_issue.get("html_url")
            log.info("Issue criada em %s: %s", target_repo, target_issue_url)
        except Exception as e:
            log.error("Falha ao criar issue em %s: %s", target_repo, e)

    comment = _build_origin_comment(result, target_issue_url)
    try:
        comment_on_issue(REPORTS_REPO, number, comment)
    except Exception as e:
        log.error("Falha ao comentar na issue #%d: %s", number, e)

    add_labels = [TRIAGED_LABEL]
    if confidence < UNDEFINED_CONFIDENCE_THRESHOLD:
        add_labels.append("low-confidence")
    elif is_bug:
        add_labels.append(IS_BUG_LABEL)
        if target_repo:
            add_labels.append(f"repo:{target_repo}")
    else:
        add_labels.append(NOT_BUG_LABEL)

    try:
        add_issue_labels(REPORTS_REPO, number, add_labels)
    except Exception as e:
        log.error("Falha ao adicionar labels: %s", e)

    if target_issue_url:
        try:
            close_issue(REPORTS_REPO, number)
        except Exception as e:
            log.error("Falha ao fechar issue #%d: %s", number, e)


def _build_target_issue_body(origin: dict, result: dict) -> str:
    files = result.get("files_analyzed", []) or []
    files_lines = "\n".join(f"- `{f}`" for f in files) or "_(nenhum arquivo)_"
    suggested = result.get("suggested_fix") or "_(sem sugestão)_"

    return (
        f"## Bug reportado via ShopFlow\n\n"
        f"**Issue original:** {origin.get('html_url', '?')}\n"
        f"**Título:** {origin.get('title', '')}\n\n"
        f"**Descrição original:**\n\n"
        f"{origin.get('body') or '_(sem descrição)_'}\n\n"
        f"---\n\n"
        f"## Análise automática (Claude Code)\n\n"
        f"**Confiança:** {result.get('confidence', 0):.0%}\n\n"
        f"**Resumo:** {result.get('summary', '')}\n\n"
        f"**Explicação técnica:**\n\n"
        f"{result.get('explanation', '')}\n\n"
        f"**Arquivos analisados:**\n\n"
        f"{files_lines}\n\n"
        f"**Sugestão de correção:**\n\n"
        f"{suggested}\n\n"
        f"---\n_Issue criada automaticamente pelo Bug Triage Pipeline._"
    )


def _build_origin_comment(result: dict, target_issue_url: str | None) -> str:
    confidence = float(result.get("confidence") or 0)
    if confidence < UNDEFINED_CONFIDENCE_THRESHOLD:
        verdict = f"**Análise inconclusiva** (confiança {confidence:.0%})"
    elif result.get("is_bug"):
        verdict = f"**Classificado como BUG** (confiança {confidence:.0%})"
    else:
        verdict = f"**Não identificado como bug** (confiança {confidence:.0%})"

    parts = [verdict, "", result.get("user_reply", "")]
    if target_issue_url:
        parts.extend(["", f"Issue técnica: {target_issue_url}"])
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== Bug Triage Pipeline (MCP) ===")
    log.info("Reports repo: %s", REPORTS_REPO)
    log.info("Repos-alvo: %s", ", ".join(TARGET_REPOS))

    issues: list[dict]
    if TRIGGERING_ISSUE and not FORCE_ALL:
        log.info("Modo evento — processando issue #%d", TRIGGERING_ISSUE)
        issue = get_issue(REPORTS_REPO, TRIGGERING_ISSUE)
        labels = [l["name"] for l in issue.get("labels", [])]
        if TRIAGE_LABEL not in labels:
            log.info(
                "Issue #%d não tem label %s (tem: %s) — pulando",
                TRIGGERING_ISSUE,
                TRIAGE_LABEL,
                labels,
            )
            return
        issues = [issue]
    else:
        log.info("Modo lote — buscando issues abertas com label %s", TRIAGE_LABEL)
        issues = list_open_issues_with_label(REPORTS_REPO, TRIAGE_LABEL)

    log.info("Issues para processar: %d", len(issues))

    if MAX_ISSUES > 0 and len(issues) > MAX_ISSUES:
        log.info("Limitando a %d (MAX_ISSUES=%d)", MAX_ISSUES, MAX_ISSUES)
        issues = issues[:MAX_ISSUES]

    for issue in issues:
        try:
            process_issue(issue)
        except Exception as e:
            log.exception("Erro ao processar issue #%s: %s", issue.get("number"), e)

    log.info("=== Triagem concluída. %d issue(s) processada(s). ===", len(issues))


if __name__ == "__main__":
    main()
