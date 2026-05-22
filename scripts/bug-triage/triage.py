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
     MCP server local que expõe a tool `submit_triage` (aceita LISTA
     de findings — suporta reports com múltiplos bugs)
  5. Lê o veredito de /tmp/triage_result.json (gravado pela tool)
  6. Pra cada finding com is_bug + confidence >= 0.5: cria issue no repo
  7. Posta UM comentário consolidado na issue original e fecha se houve
     ao menos 1 issue técnica criada
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
    """Remove uma label específica. Retorna False se já não estava lá."""
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

## Report para análise

**Título:** {title}

**Descrição:**
{body}

---

Analise o report acima navegando pelos repositórios em ./repos/. Identifique
quantos bugs distintos o usuário cita (a maioria dos reports tem 1). Quando
tiver o veredito, chame a tool `submit_triage` (UMA vez) com a lista de
findings e o `user_reply` consolidado, depois encerre.
"""

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
            "Claude Code não chamou submit_triage — provavelmente estourou turns.",
        )

    try:
        data = json.loads(TRIAGE_OUTPUT_FILE.read_text())
        findings = data.get("findings", [])
        log.info("Veredito via MCP: %d finding(s)", len(findings))
        for i, f in enumerate(findings):
            log.info(
                "  finding[%d]: is_bug=%s conf=%.2f target=%s — %s",
                i,
                f.get("is_bug"),
                f.get("confidence", 0.0),
                f.get("target_repo"),
                (f.get("summary") or "")[:80],
            )
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.error("Falha lendo output MCP: %s", e)
        return _fallback_result(title, f"Output MCP inválido: {e}")


def _fallback_result(title: str, reason: str) -> dict:
    return {
        "findings": [
            {
                "is_bug": False,
                "confidence": 0.0,
                "target_repo": None,
                "files_analyzed": [],
                "summary": f"Análise automática falhou para: {title}",
                "explanation": reason,
                "suggested_fix": None,
            }
        ],
        "user_reply": (
            f"Não consegui analisar este report automaticamente. Motivo: "
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

    if not remove_issue_label_atomic(REPORTS_REPO, number, TRIAGE_LABEL):
        log.info(
            "Issue #%d: label %s já removida — outro runner processou. Skip.",
            number,
            TRIAGE_LABEL,
        )
        return

    log.info("=== Processando issue #%d: %s ===", number, title)

    result = run_claude_triage(title, body)
    findings = result.get("findings", [])
    user_reply = result.get("user_reply", "")

    # Cria issue técnica pra cada finding confirmado como bug
    target_issues: list[tuple[dict, str]] = []  # (finding, issue_url)
    for finding in findings:
        confidence = float(finding.get("confidence") or 0)
        if (
            finding.get("is_bug")
            and finding.get("target_repo")
            and confidence >= UNDEFINED_CONFIDENCE_THRESHOLD
        ):
            try:
                target_issue = create_issue(
                    f"{GITHUB_ORG}/{finding['target_repo']}",
                    f"[Auto-Triage] {finding.get('summary') or title}",
                    _build_target_issue_body(issue, finding),
                    labels=["bug", "auto-triage"],
                )
                target_issues.append((finding, target_issue.get("html_url", "?")))
                log.info(
                    "Issue criada em %s: %s",
                    finding["target_repo"],
                    target_issue.get("html_url"),
                )
            except Exception as e:
                log.error(
                    "Falha ao criar issue em %s: %s",
                    finding.get("target_repo"),
                    e,
                )

    # Posta UM comentário consolidado na issue original
    comment = _build_origin_comment(findings, user_reply, target_issues)
    try:
        comment_on_issue(REPORTS_REPO, number, comment)
    except Exception as e:
        log.error("Falha ao comentar na issue #%d: %s", number, e)

    # Labels agregadas
    add_labels = _aggregate_labels(findings, target_issues)
    try:
        add_issue_labels(REPORTS_REPO, number, add_labels)
    except Exception as e:
        log.error("Falha ao adicionar labels: %s", e)

    # Fecha se ao menos 1 issue técnica foi criada
    if target_issues:
        try:
            close_issue(REPORTS_REPO, number)
        except Exception as e:
            log.error("Falha ao fechar issue #%d: %s", number, e)


def _aggregate_labels(findings: list[dict], target_issues: list[tuple]) -> list[str]:
    labels = [TRIAGED_LABEL]

    high_conf_bugs = [
        f for f in findings
        if f.get("is_bug")
        and float(f.get("confidence", 0)) >= UNDEFINED_CONFIDENCE_THRESHOLD
    ]
    any_bug_attempted = any(f.get("is_bug") for f in findings)

    if high_conf_bugs:
        labels.append(IS_BUG_LABEL)
        repos_seen: set[str] = set()
        for f in high_conf_bugs:
            repo = f.get("target_repo")
            if repo and repo not in repos_seen:
                repos_seen.add(repo)
                labels.append(f"repo:{repo}")
    elif any_bug_attempted:
        labels.append("low-confidence")
    else:
        labels.append(NOT_BUG_LABEL)

    return labels


def _build_target_issue_body(origin: dict, finding: dict) -> str:
    files = finding.get("files_analyzed", []) or []
    files_lines = "\n".join(f"- `{f}`" for f in files) or "_(nenhum arquivo)_"
    suggested = finding.get("suggested_fix") or "_(sem sugestão)_"

    return (
        f"## Bug reportado via ShopFlow\n\n"
        f"**Issue original:** {origin.get('html_url', '?')}\n"
        f"**Título do report:** {origin.get('title', '')}\n\n"
        f"**Descrição original do reportador:**\n\n"
        f"{origin.get('body') or '_(sem descrição)_'}\n\n"
        f"---\n\n"
        f"## Análise automática (Claude Code)\n\n"
        f"**Confiança:** {finding.get('confidence', 0):.0%}\n\n"
        f"**Resumo:** {finding.get('summary', '')}\n\n"
        f"**Explicação técnica:**\n\n"
        f"{finding.get('explanation', '')}\n\n"
        f"**Arquivos analisados:**\n\n"
        f"{files_lines}\n\n"
        f"**Sugestão de correção:**\n\n"
        f"{suggested}\n\n"
        f"---\n_Issue criada automaticamente pelo Bug Triage Pipeline._"
    )


def _build_origin_comment(
    findings: list[dict],
    user_reply: str,
    target_issues: list[tuple],
) -> str:
    high_conf_bugs = [
        f for f in findings
        if f.get("is_bug")
        and float(f.get("confidence", 0)) >= UNDEFINED_CONFIDENCE_THRESHOLD
    ]
    any_bug_attempted = any(f.get("is_bug") for f in findings)

    # Header
    if not findings:
        header = "**Análise inconclusiva**"
    elif len(high_conf_bugs) == 0:
        if any_bug_attempted:
            header = "**Análise inconclusiva** — bugs identificados mas com baixa confiança"
        else:
            header = "**Não identificado como bug**"
    elif len(high_conf_bugs) == 1:
        f = high_conf_bugs[0]
        header = f"**Classificado como BUG** (confiança {float(f['confidence']):.0%})"
    else:
        header = f"**{len(high_conf_bugs)} bugs identificados:**"
        lines = []
        for i, f in enumerate(high_conf_bugs, 1):
            lines.append(
                f"{i}. {f.get('summary', '(sem resumo)')} "
                f"({float(f['confidence']):.0%} — `{f.get('target_repo', '?')}`)"
            )
        header += "\n\n" + "\n".join(lines)

    parts = [header, "", user_reply]

    if target_issues:
        parts.append("")
        if len(target_issues) == 1:
            parts.append(f"Issue técnica: {target_issues[0][1]}")
        else:
            parts.append("Issues técnicas:")
            for finding, url in target_issues:
                parts.append(f"- `{finding.get('target_repo', '?')}`: {url}")

    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== Bug Triage Pipeline (MCP + multi-findings) ===")
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
