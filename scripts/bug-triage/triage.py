"""
Bug Triage Automático via GitHub Issues + Claude Code Headless.

Demo da palestra "Triagem Autônoma de Bugs com Claude Code Headless" (BaIA).

O usuário reporta um bug pelo form web da ShopFlow, que abre uma issue em
`baia-demo/bug-reports` com label `needs-triage`. Este script é disparado
pelo GitHub Actions e:

  1. Lê o título e corpo da issue
  2. Clona os repositórios da arquitetura (shallow)
  3. Dispara Claude Code em modo headless (read-only)
  4. Recebe um JSON com veredito + confiança + repo + análise
  5. Se for bug: cria issue no repo correto e linka na issue original
  6. Atualiza labels, comenta na issue original e fecha

Modos:
  - Disparo por evento (`issues.labeled`): processa apenas a issue do evento
  - `workflow_dispatch`: processa todas as issues abertas com label de triagem
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
# Config (tudo via env — ver README.md)
# ---------------------------------------------------------------------------

GH_PAT = os.environ["GH_PAT"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_ORG = os.environ["GITHUB_ORG"]
REPORTS_REPO = os.environ["REPORTS_REPO"]              # ex: baia-demo/bug-reports
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
MAX_ISSUES = int(os.environ.get("MAX_ISSUES", "0"))     # 0 = sem limite

CLAUDE_TRIAGE_MD = Path(__file__).parent / "CLAUDE_TRIAGE.md"
CLAUDE_TIMEOUT = 480  # 8 min por issue

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
) -> dict | list:
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

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as e:
        log.error("GitHub API %s %s falhou (%s): %s", method, path, e.code, e.read().decode()[:500])
        raise


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


def replace_issue_labels(repo: str, number: int, labels: list[str]) -> None:
    _github_request(
        "PUT",
        f"/repos/{repo}/issues/{number}/labels",
        body={"labels": labels},
    )


def close_issue(repo: str, number: int) -> None:
    _github_request(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        body={"state": "closed", "state_reason": "completed"},
    )


# ---------------------------------------------------------------------------
# Claude Code headless
# ---------------------------------------------------------------------------


def run_claude_triage(title: str, body: str) -> dict:
    instructions = CLAUDE_TRIAGE_MD.read_text()

    prompt = f"""{instructions}

---

## Bug para análise

**Título:** {title}

**Descrição:**
{body}

---

Analise o bug acima navegando pelos repositórios em ./repos/ e responda com o JSON especificado.
"""

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--max-turns", "25",
                "--model", "claude-sonnet-4-6",
                "--allowedTools", "Read,Glob,Grep,LS",
            ],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            cwd=str(Path.cwd()),
            env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        )
    except subprocess.TimeoutExpired:
        log.error("Claude Code timeout após %ds", CLAUDE_TIMEOUT)
        return _fallback_result(title, "Timeout na análise — bug complexo demais.")

    try:
        outer = json.loads(result.stdout)
        cost = outer.get("total_cost_usd", 0)
        turns = outer.get("num_turns", 0)
        duration = outer.get("duration_ms", 0) / 1000
        log.info("Claude Code: %d turns, %.1fs, $%.4f", turns, duration, cost)
    except (json.JSONDecodeError, TypeError):
        pass

    if result.returncode != 0:
        log.error("Claude Code rc=%d stderr: %s", result.returncode, result.stderr[:500])
        return _fallback_result(
            title,
            f"Erro na execução do Claude Code: {result.stderr[:200] or result.stdout[:200]}",
        )

    return _parse_claude_output(result.stdout, title)


def _resume_for_json(session_id: str) -> str:
    """Retoma uma sessão pedindo apenas o JSON final."""
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "Pare de explorar. Retorne AGORA o JSON da triagem com os campos: "
                "is_bug, confidence, target_repo, files_analyzed, summary, "
                "explanation, suggested_fix, user_reply. Baseie-se no que já analisou.",
                "--output-format", "json",
                "--max-turns", "3",
                "--model", "claude-sonnet-4-6",
                "--resume", session_id,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path.cwd()),
            env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        )
        if result.returncode == 0:
            outer = json.loads(result.stdout)
            return outer.get("result", "")
    except Exception as e:
        log.warning("Falha ao resumir sessão: %s", e)
    return ""


def _try_parse(response_text: str) -> dict | None:
    """Tenta extrair e validar o JSON do output. Retorna o dict ou None."""
    json_str = _extract_json(response_text)
    if not json_str:
        return None
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    required = ["is_bug", "confidence", "target_repo", "summary", "user_reply"]
    if not all(k in parsed for k in required):
        return None

    raw_repo = parsed.get("target_repo")
    if raw_repo:
        normalized = str(raw_repo).strip().lower().split("/")[-1]
        if normalized in TARGET_REPOS:
            parsed["target_repo"] = normalized
        elif raw_repo not in TARGET_REPOS:
            log.warning(
                "target_repo inválido '%s', fallback: %s",
                raw_repo,
                TARGET_REPOS[0],
            )
            parsed["target_repo"] = TARGET_REPOS[0]
    return parsed


def _parse_claude_output(raw_output: str, title: str) -> dict:
    session_id = ""
    subtype = ""
    try:
        outer = json.loads(raw_output)
        subtype = outer.get("subtype", "")
        session_id = outer.get("session_id", "")
        response_text = outer.get("result", "")
    except (json.JSONDecodeError, TypeError):
        response_text = raw_output

    parsed = _try_parse(response_text) if response_text else None
    if parsed:
        return parsed

    # Primeira tentativa falhou — tenta resumir a sessão pedindo só o JSON.
    # Cobre tanto max_turns (sem result) quanto JSON malformado/cortado.
    if session_id:
        log.info(
            "Parse falhou (subtype=%s, len=%d). Resumindo sessão para JSON limpo...",
            subtype,
            len(response_text or ""),
        )
        resumed = _resume_for_json(session_id)
        parsed = _try_parse(resumed) if resumed else None
        if parsed:
            return parsed
        log.warning("Resume também não retornou JSON válido. preview: %s", (resumed or "")[:300])

    log.warning("Não foi possível parsear output. preview: %s", (response_text or "")[:500])
    return _fallback_result(title, f"Análise inconclusiva: {(response_text or '')[:500]}")


def _extract_json(text: str) -> str | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped

    for marker in ["```json\n", "```\n"]:
        if marker in text:
            start = text.index(marker) + len(marker)
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        return text[first : last + 1]

    return None


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
            f"Não consegui analisar este bug automaticamente. Motivo: {reason[:200]}. "
            "Vai precisar de uma olhada manual."
        ),
    }


# ---------------------------------------------------------------------------
# Processamento de uma issue
# ---------------------------------------------------------------------------


UNDEFINED_CONFIDENCE_THRESHOLD = 0.5


def process_issue(issue: dict) -> None:
    title = issue.get("title", "(sem título)")
    body = issue.get("body") or "(sem descrição)"
    number = issue["number"]

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

    labels = [TRIAGED_LABEL]
    if confidence < UNDEFINED_CONFIDENCE_THRESHOLD:
        labels.append("low-confidence")
    elif is_bug:
        labels.append(IS_BUG_LABEL)
        if target_repo:
            labels.append(f"repo:{target_repo}")
    else:
        labels.append(NOT_BUG_LABEL)

    try:
        replace_issue_labels(REPORTS_REPO, number, labels)
    except Exception as e:
        log.error("Falha ao atualizar labels: %s", e)

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
    log.info("=== Bug Triage Pipeline ===")
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
