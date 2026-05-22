"""
Triage Pipeline (BaIA demo) — GitHub Issues + Claude Code headless + MCP.

Roda no GitHub Actions, dispara Claude Code em modo headless conectado a um
MCP server local que expõe a tool `submit_triage(findings, user_reply)`.

Cada finding tem um `kind` (bug/improvement/question/unclear/rejected).

  - bug/improvement com confidence >= 0.5: viram issue técnica no target_repo
  - bug/improvement com confidence >= 0.9: AUTO-FIX (Claude com Edit/Write
    aplica o fix, abre PR, espera CI, mergeia)
  - rejected (conf >= 0.7): comenta na origem explicando, fecha como not_planned
  - question/unclear: só comenta

Dedupe semântica: ANTES de criar issue técnica, lista issues abertas com
label `auto-triage` no target_repo e usa Claude (Haiku) pra checar se o
novo finding é duplicata. Se for, comenta na issue existente e fecha a
user-feedback ao invés de criar nova.

Métricas: cost USD, turns, duração acumulados ao longo de todas chamadas
Claude (triage + dedup + auto-fix) — visíveis no log, no GH Actions
summary e no comentário final na issue de origem.
"""

import json
import logging
import os
import re
import subprocess
import time
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
REPOS_DIR = Path("repos")
CLAUDE_TIMEOUT = 480
CLAUDE_FIX_TIMEOUT = 600
DEDUP_TIMEOUT = 180

UNDEFINED_CONFIDENCE_THRESHOLD = 0.5
REJECTION_CONFIDENCE_THRESHOLD = 0.7
AUTO_FIX_CONFIDENCE_THRESHOLD = 0.9
AUTO_FIX_BRANCH_PREFIX = "auto-fix/"
AUTO_FIX_KINDS = {"bug", "improvement"}
AUTO_TRIAGE_LABEL = "auto-triage"  # aplicado em issues técnicas (usado pra dedup)

KIND_TO_TARGET_LABELS = {
    "bug": ["bug", AUTO_TRIAGE_LABEL],
    "improvement": ["enhancement", AUTO_TRIAGE_LABEL],
}

KIND_TO_ORIGIN_LABELS = {
    "bug": "is-bug",
    "improvement": "is-improvement",
    "question": "is-question",
    "unclear": "needs-info",
    "rejected": "rejected",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("triage")


# ---------------------------------------------------------------------------
# Métricas acumuladas durante o processamento de uma issue
# ---------------------------------------------------------------------------


def new_metrics() -> dict:
    return {"cost_usd": 0.0, "turns": 0, "duration_s": 0.0, "calls": 0}


def accumulate(metrics: dict, call_label: str, outer_json: dict | None) -> None:
    if not outer_json:
        return
    cost = float(outer_json.get("total_cost_usd", 0) or 0)
    turns = int(outer_json.get("num_turns", 0) or 0)
    duration = float(outer_json.get("duration_ms", 0) or 0) / 1000
    metrics["cost_usd"] += cost
    metrics["turns"] += turns
    metrics["duration_s"] += duration
    metrics["calls"] += 1
    log.info(
        "Métrica [%s]: %d turns, %.1fs, $%.4f (acumulado: $%.4f em %d call(s))",
        call_label, turns, duration, cost, metrics["cost_usd"], metrics["calls"],
    )


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


def create_pull_request(
    repo: str, title: str, head: str, base: str, body: str
) -> dict:
    return _github_request(
        "POST",
        f"/repos/{repo}/pulls",
        body={"title": title, "head": head, "base": base, "body": body},
    )  # type: ignore[return-value]


def wait_for_pr_checks(repo: str, pr_number: int, timeout: int = 900) -> bool:
    start = time.time()
    last_state: list[str] = []
    while time.time() - start < timeout:
        try:
            pr = _github_request("GET", f"/repos/{repo}/pulls/{pr_number}")
            sha = pr["head"]["sha"]  # type: ignore[index]
            runs_data = _github_request(
                "GET", f"/repos/{repo}/commits/{sha}/check-runs"
            )
            check_runs = runs_data.get("check_runs", [])  # type: ignore[union-attr]
        except Exception as e:
            log.warning("Falha consultando PR/checks: %s", e)
            time.sleep(15)
            continue

        if not check_runs:
            log.info("PR #%d: nenhum check ainda; aguardando...", pr_number)
            time.sleep(15)
            continue

        current = sorted(
            f"{r['name']}={r['status']}/{r.get('conclusion', '?')}"
            for r in check_runs
        )
        if current != last_state:
            log.info("PR #%d checks: %s", pr_number, ", ".join(current))
            last_state = current

        all_done = all(r["status"] == "completed" for r in check_runs)
        if not all_done:
            time.sleep(15)
            continue

        ok_outcomes = {"success", "skipped", "neutral"}
        failed = [r["name"] for r in check_runs if r.get("conclusion") not in ok_outcomes]
        if failed:
            log.warning("PR #%d: checks falharam: %s", pr_number, failed)
            return False
        return True

    log.error("PR #%d: timeout esperando checks após %ds", pr_number, timeout)
    return False


def merge_pr(repo: str, pr_number: int) -> bool:
    try:
        result = _github_request(
            "PUT",
            f"/repos/{repo}/pulls/{pr_number}/merge",
            body={"merge_method": "squash"},
        )
        if not result or not result.get("merged"):  # type: ignore[union-attr]
            log.warning("Merge API não confirmou: %s", result)
            return False
        try:
            pr = _github_request("GET", f"/repos/{repo}/pulls/{pr_number}")
            branch = pr["head"]["ref"]  # type: ignore[index]
            _github_request("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
        except Exception as e:
            log.debug("Falha deletando branch (não-crítico): %s", e)
        return True
    except Exception as e:
        log.error("Merge falhou: %s", e)
        return False


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
    quoted = urllib.parse.quote(label, safe="")
    try:
        _github_request("DELETE", f"/repos/{repo}/issues/{number}/labels/{quoted}")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def close_issue(repo: str, number: int, reason: str = "completed") -> None:
    _github_request(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        body={"state": "closed", "state_reason": reason},
    )


# ---------------------------------------------------------------------------
# Dedup semântica (LLM-based, Haiku)
# ---------------------------------------------------------------------------


def find_duplicate_issue(
    target_repo: str,
    summary: str,
    kind: str,
    metrics: dict,
) -> dict | None:
    """Verifica se o novo finding duplica alguma issue auto-triage aberta no target_repo.

    Retorna a issue dict (dict do GitHub) ou None.
    """
    candidates = list_open_issues_with_label(
        f"{GITHUB_ORG}/{target_repo}", AUTO_TRIAGE_LABEL
    )
    if not candidates:
        log.info("Dedup [%s]: nenhuma issue auto-triage aberta — skip", target_repo)
        return None

    log.info(
        "Dedup [%s]: comparando finding com %d candidata(s) abertas",
        target_repo,
        len(candidates),
    )

    issues_block = "\n".join(
        f"#{i['number']}: {i.get('title', '')}\n"
        f"   Excerpt: {((i.get('body') or '')[:300]).strip()}"
        for i in candidates[:8]
    )

    prompt = f"""Você compara se um report novo é duplicado de issues existentes.

NEW FINDING:
  kind: {kind}
  summary: {summary}

EXISTING OPEN auto-triage ISSUES in {target_repo}:
{issues_block}

PERGUNTA: o NEW finding descreve a MESMA causa raiz que alguma das existentes?

Responda com EXATAMENTE uma palavra:
- O número da issue (só o inteiro, sem '#'), se for duplicata
- "none", se NÃO for duplicata de nenhuma

Seja conservador: só marque como duplicata se tiver certeza que a causa raiz é a mesma.
Não explique. Só o número ou "none".
"""

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--max-turns", "1",
                "--model", "claude-haiku-4-5",
            ],
            capture_output=True,
            text=True,
            timeout=DEDUP_TIMEOUT,
            env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        )
    except subprocess.TimeoutExpired:
        log.warning("Dedup timeout — não consegui checar duplicatas")
        return None

    if result.returncode != 0:
        log.warning("Dedup rc=%d stderr: %s", result.returncode, result.stderr[:200])
        return None

    try:
        outer = json.loads(result.stdout)
        accumulate(metrics, "dedup", outer)
        response_text = (outer.get("result") or "").strip().lower()
    except (json.JSONDecodeError, TypeError):
        log.warning("Dedup output não-JSON: %s", result.stdout[:200])
        return None

    if not response_text or "none" in response_text:
        log.info("Dedup [%s]: sem duplicata", target_repo)
        return None

    match = re.search(r"\b(\d+)\b", response_text)
    if not match:
        log.warning("Dedup output sem número: %s", response_text[:100])
        return None

    dup_number = int(match.group(1))
    for issue in candidates:
        if issue["number"] == dup_number:
            log.info(
                "Dedup [%s]: finding é duplicata de #%d (%s)",
                target_repo,
                dup_number,
                issue.get("title", "")[:60],
            )
            return issue

    log.warning("Dedup: agente apontou #%d mas não está na lista", dup_number)
    return None


# ---------------------------------------------------------------------------
# MCP config + Claude Code headless (triagem)
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


def run_claude_triage(title: str, body: str, metrics: dict) -> dict:
    instructions = CLAUDE_TRIAGE_MD.read_text()

    prompt = f"""{instructions}

---

## Report para análise

**Título:** {title}

**Descrição:**
{body}

---

Analise o report navegando pelos repositórios em ./repos/ se necessário.
Comece classificando os pontos do report (bug/improvement/question/unclear/rejected).
Investigue código somente pra bug/improvement. Quando tiver o veredito,
chame `submit_triage` UMA vez com os findings e o user_reply, depois encerre.
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
        log.error("Claude triagem timeout após %ds", CLAUDE_TIMEOUT)
        return _fallback_result(title, "Timeout na análise.")

    try:
        outer = json.loads(result.stdout)
        accumulate(metrics, "triage", outer)
    except (json.JSONDecodeError, TypeError):
        outer = None

    if result.returncode != 0:
        log.error("Claude triagem rc=%d stderr: %s", result.returncode, result.stderr[:500])

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
                "  finding[%d]: kind=%s conf=%.2f target=%s — %s",
                i,
                f.get("kind"),
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
                "kind": "unclear",
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
# Auto-fix
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: str | Path, **kwargs) -> subprocess.CompletedProcess:
    log.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=str(cwd), check=True, **kwargs)


def run_auto_fix(
    finding: dict,
    origin_issue: dict,
    target_issue: dict,
    metrics: dict,
) -> str | None:
    repo = finding["target_repo"]
    work_dir = REPOS_DIR / repo
    if not work_dir.exists():
        log.error("Repo clonado não encontrado: %s", work_dir)
        return None

    branch = f"{AUTO_FIX_BRANCH_PREFIX}feedback-{origin_issue['number']}-{int(time.time())}"
    log.info("Auto-fix em %s: branch %s", repo, branch)

    try:
        try:
            _run(["git", "fetch", "--unshallow"], cwd=work_dir,
                 capture_output=True, text=True)
        except subprocess.CalledProcessError:
            pass

        _run(["git", "config", "user.email", "auto-fix-agent@baia-demo.dev"], cwd=work_dir)
        _run(["git", "config", "user.name", "Auto-fix Agent (Claude)"], cwd=work_dir)
        _run(["git", "checkout", "-b", branch], cwd=work_dir,
             capture_output=True, text=True)

        fix_prompt = _build_fix_prompt(repo, origin_issue, target_issue, finding)
        log.info("Executando Claude (fix mode) em %s...", work_dir)
        result = subprocess.run(
            [
                "claude",
                "-p", fix_prompt,
                "--output-format", "json",
                "--max-turns", "30",
                "--model", "claude-sonnet-4-6",
                "--allowedTools", "Read,Glob,Grep,LS,Edit,Write,Bash",
            ],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=CLAUDE_FIX_TIMEOUT,
            env={**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY},
        )

        try:
            outer = json.loads(result.stdout)
            accumulate(metrics, f"fix:{repo}", outer)
        except (json.JSONDecodeError, TypeError):
            pass

        if result.returncode != 0:
            log.error("Claude fix rc=%d stderr: %s", result.returncode, result.stderr[:500])
            return None

        diff = subprocess.run(
            ["git", "diff", "--stat"], cwd=str(work_dir),
            capture_output=True, text=True,
        )
        if not diff.stdout.strip():
            log.warning("Claude não fez mudanças no código — abortando PR")
            return None

        log.info("Diff:\n%s", diff.stdout[:1000])

        _run(["git", "add", "-A"], cwd=work_dir)
        commit_msg = _build_commit_message(finding, origin_issue, target_issue)
        _run(["git", "commit", "-m", commit_msg], cwd=work_dir,
             capture_output=True, text=True)

        push_url = f"https://x-access-token:{GH_PAT}@github.com/{GITHUB_ORG}/{repo}.git"
        _run(["git", "push", push_url, f"HEAD:{branch}"], cwd=work_dir,
             capture_output=True, text=True)

        pr = create_pull_request(
            f"{GITHUB_ORG}/{repo}",
            title=f"{_pr_title_prefix(finding)} {finding.get('summary', '')}"[:100],
            head=branch,
            base="main",
            body=_build_pr_body(finding, origin_issue, target_issue),
        )
        pr_url = pr.get("html_url")
        pr_number = pr.get("number")
        log.info("PR aberta: %s", pr_url)

        if pr_number:
            log.info("Aguardando checks da PR #%s...", pr_number)
            ok = wait_for_pr_checks(f"{GITHUB_ORG}/{repo}", pr_number, timeout=900)
            if ok:
                log.info("Checks verdes — mergeando PR #%s", pr_number)
                merge_pr(f"{GITHUB_ORG}/{repo}", pr_number)
            else:
                log.warning("Checks falharam — PR fica aberta pra revisão manual")
        return pr_url
    except subprocess.CalledProcessError as e:
        log.error("Comando falhou no auto-fix: %s | stderr=%s", e, e.stderr if hasattr(e, "stderr") else "")
        return None
    except subprocess.TimeoutExpired:
        log.error("Claude fix timeout após %ds", CLAUDE_FIX_TIMEOUT)
        return None
    except Exception as e:
        log.exception("Erro inesperado no auto-fix: %s", e)
        return None


def _build_fix_prompt(repo: str, origin: dict, target: dict, finding: dict) -> str:
    suggested = finding.get("suggested_fix") or "(o agente de triagem não sugeriu fix específico)"
    return f"""Você é um engenheiro corrigindo um bug/melhoria identificado pelo agente de triagem.

**Repositório:** `{repo}` (você está dentro do clone)

**Issue técnica:** #{target.get('number', '?')} — {target.get('title', '')}
**Issue original do usuário:** {origin.get('html_url', '?')}

**Diagnóstico do agente de triagem:**

- Tipo: `{finding.get('kind')}`
- Resumo: {finding.get('summary', '')}
- Confiança: {finding.get('confidence', 0):.0%}
- Arquivos analisados: {', '.join(finding.get('files_analyzed', [])) or '(nenhum)'}

**Explicação técnica:**

{finding.get('explanation', '')}

**Sugestão de fix (use como guia, não obrigatório literal):**

{suggested}

---

## Sua tarefa

1. Aplique o fix nos arquivos relevantes. Mantenha o escopo MÍNIMO — não
   refatore coisas não relacionadas. Mude só o necessário.
2. Se existirem tests cobrindo a área, garanta que continuam passando.
   Se NÃO existir teste cobrindo o caso específico do bug, adicione um
   teste mínimo (junto com o fix) que reproduziria o bug e agora passa.
3. Rode `npm test` pra confirmar que tudo passa antes de terminar.
4. NÃO commite — só edite. O workflow que te chamou vai commitar e abrir PR.
5. Quando terminar, descreva em 2-3 linhas o que fez (texto livre).

Restrição de escopo: limite-se a `{repo}`. Não toque em outros repos.
"""


def _build_commit_message(finding: dict, origin: dict, target: dict) -> str:
    prefix = "fix" if finding.get("kind") == "bug" else "feat"
    summary = finding.get("summary", "(sem resumo)")[:72]
    return (
        f"{prefix}: {summary}\n\n"
        f"Auto-fix em resposta à issue técnica #{target.get('number')}.\n"
        f"Reportado originalmente em {origin.get('html_url', '?')}\n\n"
        f"Closes #{target.get('number')}"
    )


def _pr_title_prefix(finding: dict) -> str:
    return "fix:" if finding.get("kind") == "bug" else "feat:"


def _build_pr_body(finding: dict, origin: dict, target: dict) -> str:
    kind_pt = {"bug": "bug", "improvement": "melhoria"}.get(
        finding.get("kind", ""), "report"
    )
    return (
        f"Auto-{kind_pt}-fix aplicado pelo Claude Code Agent em resposta à "
        f"issue técnica #{target.get('number')}.\n\n"
        f"**Reporte original:** {origin.get('html_url', '?')}\n"
        f"**Confiança da triagem:** {finding.get('confidence', 0):.0%}\n\n"
        f"## Diagnóstico\n\n"
        f"{finding.get('explanation', '')}\n\n"
        f"## Fix sugerido pela triagem\n\n"
        f"{finding.get('suggested_fix') or '_(sem sugestão específica)_'}\n\n"
        f"---\n\n"
        f"Closes #{target.get('number')}.\n"
    )


# ---------------------------------------------------------------------------
# Processamento de uma issue
# ---------------------------------------------------------------------------


def _is_actionable(finding: dict) -> bool:
    kind = finding.get("kind")
    confidence = float(finding.get("confidence") or 0)
    return (
        kind in ("bug", "improvement")
        and finding.get("target_repo")
        and confidence >= UNDEFINED_CONFIDENCE_THRESHOLD
    )


def _is_auto_fix_eligible(finding: dict) -> bool:
    kind = finding.get("kind")
    confidence = float(finding.get("confidence") or 0)
    return (
        kind in AUTO_FIX_KINDS
        and finding.get("target_repo")
        and confidence >= AUTO_FIX_CONFIDENCE_THRESHOLD
    )


def _is_rejection(finding: dict) -> bool:
    kind = finding.get("kind")
    confidence = float(finding.get("confidence") or 0)
    return kind == "rejected" and confidence >= REJECTION_CONFIDENCE_THRESHOLD


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
    metrics = new_metrics()

    result = run_claude_triage(title, body, metrics)
    findings = result.get("findings", [])
    user_reply = result.get("user_reply", "")

    target_issues: list[tuple[dict, dict]] = []
    duplicates: list[tuple[dict, dict]] = []
    fix_prs: list[tuple[dict, str]] = []

    for finding in findings:
        if not _is_actionable(finding):
            continue
        kind = finding["kind"]
        target_repo = finding["target_repo"]

        dup = find_duplicate_issue(target_repo, finding.get("summary", ""), kind, metrics)
        if dup:
            try:
                comment_on_issue(
                    f"{GITHUB_ORG}/{target_repo}",
                    dup["number"],
                    _build_duplicate_comment(issue, finding),
                )
                duplicates.append((finding, dup))
                log.info("Anexado como duplicata em %s#%d", target_repo, dup["number"])
            except Exception as e:
                log.error("Falha comentando na issue duplicada: %s", e)
            continue

        try:
            target_issue = create_issue(
                f"{GITHUB_ORG}/{target_repo}",
                _target_title(kind, finding, title),
                _build_target_issue_body(issue, finding),
                labels=KIND_TO_TARGET_LABELS[kind],
            )
            target_issues.append((finding, target_issue))
            log.info(
                "Issue %s criada em %s: %s",
                kind,
                target_repo,
                target_issue.get("html_url"),
            )
        except Exception as e:
            log.error("Falha ao criar issue em %s (%s): %s", target_repo, kind, e)

    for finding, target_issue in target_issues:
        if not _is_auto_fix_eligible(finding):
            continue
        log.info(
            "Auto-fix elegível: %s em %s (conf %.2f)",
            finding.get("summary"),
            finding["target_repo"],
            finding.get("confidence", 0),
        )
        try:
            pr_url = run_auto_fix(finding, issue, target_issue, metrics)
            if pr_url:
                fix_prs.append((finding, pr_url))
        except Exception as e:
            log.exception("Auto-fix falhou: %s", e)

    comment = _build_origin_comment(
        findings, user_reply, target_issues, duplicates, fix_prs, metrics
    )
    try:
        comment_on_issue(REPORTS_REPO, number, comment)
    except Exception as e:
        log.error("Falha ao comentar na issue #%d: %s", number, e)

    add_labels = _aggregate_labels(findings, target_issues, duplicates)
    try:
        add_issue_labels(REPORTS_REPO, number, add_labels)
    except Exception as e:
        log.error("Falha ao adicionar labels: %s", e)

    should_close = (
        bool(target_issues)
        or bool(duplicates)
        or any(_is_rejection(f) for f in findings)
    )
    if should_close:
        reason = (
            "not_planned"
            if any(_is_rejection(f) for f in findings) and not target_issues
            else "completed"
        )
        try:
            close_issue(REPORTS_REPO, number, reason=reason)
        except Exception as e:
            log.error("Falha ao fechar issue #%d: %s", number, e)

    _emit_metrics_summary(number, title, metrics)


def _target_title(kind: str, finding: dict, origin_title: str) -> str:
    prefix = "[Auto-Triage]" if kind == "bug" else "[Auto-Triage: melhoria]"
    summary = finding.get("summary") or origin_title
    return f"{prefix} {summary}"


def _aggregate_labels(
    findings: list[dict],
    target_issues: list[tuple],
    duplicates: list[tuple],
) -> list[str]:
    labels: list[str] = [TRIAGED_LABEL]
    seen: set[str] = set()

    for f in findings:
        kind = f.get("kind", "unclear")
        conf = float(f.get("confidence") or 0)
        if kind == "rejected":
            if conf < REJECTION_CONFIDENCE_THRESHOLD:
                continue
        elif conf < UNDEFINED_CONFIDENCE_THRESHOLD:
            continue
        kind_label = KIND_TO_ORIGIN_LABELS.get(kind)
        if kind_label and kind_label not in seen:
            seen.add(kind_label)
            labels.append(kind_label)

    actionable = [f for f in findings if _is_actionable(f)]
    for f in actionable:
        repo = f.get("target_repo")
        if repo:
            tag = f"repo:{repo}"
            if tag not in seen:
                seen.add(tag)
                labels.append(tag)

    if duplicates:
        labels.append("duplicate-of-known")

    if any(_is_auto_fix_eligible(f) for f in findings):
        labels.append("auto-fix-eligible")

    if len(labels) == 1:
        labels.append("low-confidence")

    return labels


def _build_target_issue_body(origin: dict, finding: dict) -> str:
    files = finding.get("files_analyzed", []) or []
    files_lines = "\n".join(f"- `{f}`" for f in files) or "_(nenhum arquivo)_"
    suggested = finding.get("suggested_fix") or "_(sem sugestão)_"
    kind_pt = {
        "bug": "Bug reportado",
        "improvement": "Sugestão de melhoria",
    }.get(finding.get("kind", "bug"), "Report")

    return (
        f"## {kind_pt} via ShopFlow\n\n"
        f"**Issue original:** {origin.get('html_url', '?')}\n"
        f"**Título do report:** {origin.get('title', '')}\n\n"
        f"**Descrição original do usuário:**\n\n"
        f"{origin.get('body') or '_(sem descrição)_'}\n\n"
        f"---\n\n"
        f"## Análise automática (Claude Code)\n\n"
        f"**Tipo:** `{finding.get('kind', 'bug')}`\n\n"
        f"**Confiança:** {finding.get('confidence', 0):.0%}\n\n"
        f"**Resumo:** {finding.get('summary', '')}\n\n"
        f"**Explicação técnica:**\n\n"
        f"{finding.get('explanation', '')}\n\n"
        f"**Arquivos analisados:**\n\n"
        f"{files_lines}\n\n"
        f"**Sugestão:**\n\n"
        f"{suggested}\n\n"
        f"---\n_Issue criada automaticamente pelo Triage Pipeline._"
    )


def _build_duplicate_comment(origin: dict, finding: dict) -> str:
    return (
        f"+1 — outro usuário relatou problema similar em "
        f"{origin.get('html_url', '?')}.\n\n"
        f"**Resumo do novo relato:** {finding.get('summary', '')}\n\n"
        f"_Vinculado automaticamente pelo Triage Pipeline (dedup semântica)._"
    )


def _build_origin_comment(
    findings: list[dict],
    user_reply: str,
    target_issues: list[tuple],
    duplicates: list[tuple],
    fix_prs: list[tuple],
    metrics: dict,
) -> str:
    actionable = [f for f in findings if _is_actionable(f)]
    rejections = [f for f in findings if _is_rejection(f)]

    KIND_LABEL = {
        "bug": "BUG",
        "improvement": "MELHORIA",
        "question": "PERGUNTA",
        "unclear": "NÃO CLASSIFICADO",
        "rejected": "REJEITADO",
    }

    if not findings:
        header = "**Análise inconclusiva**"
    elif rejections and not actionable:
        if len(rejections) == 1:
            f = rejections[0]
            header = f"**Report REJEITADO** (confiança {float(f['confidence']):.0%})"
        else:
            header = f"**{len(rejections)} pontos rejeitados**"
    elif len(actionable) == 0:
        kinds = sorted({f.get("kind", "unclear") for f in findings})
        if kinds == ["question"]:
            header = "**Classificado como pergunta** (sem código pra ajustar)"
        elif kinds == ["unclear"]:
            header = "**Análise inconclusiva** — precisa de mais detalhes"
        else:
            header = "**Classificação inconclusiva** — baixa confiança em todos os pontos"
    elif len(actionable) == 1:
        f = actionable[0]
        kind_label = KIND_LABEL.get(f["kind"], f["kind"].upper())
        header = f"**Classificado como {kind_label}** (confiança {float(f['confidence']):.0%})"
    else:
        header = f"**{len(actionable)} pontos identificados:**"
        lines = []
        for i, f in enumerate(actionable, 1):
            kind_label = KIND_LABEL.get(f["kind"], f["kind"].upper())
            lines.append(
                f"{i}. {kind_label}: {f.get('summary', '(sem resumo)')} "
                f"({float(f['confidence']):.0%} — `{f.get('target_repo', '?')}`)"
            )
        header += "\n\n" + "\n".join(lines)

    parts = [header, "", user_reply]

    if target_issues:
        parts.append("")
        if len(target_issues) == 1:
            finding, ti = target_issues[0]
            parts.append(f"Issue técnica: {ti.get('html_url', '?')}")
        else:
            parts.append("Issues técnicas:")
            for finding, ti in target_issues:
                kind_label = "bug" if finding["kind"] == "bug" else "melhoria"
                parts.append(
                    f"- `{finding.get('target_repo', '?')}` ({kind_label}): {ti.get('html_url', '?')}"
                )

    if duplicates:
        parts.append("")
        if len(duplicates) == 1:
            finding, dup = duplicates[0]
            parts.append(f"Duplicata de issue existente: {dup.get('html_url', '?')}")
        else:
            parts.append("Duplicatas de issues existentes:")
            for finding, dup in duplicates:
                parts.append(f"- `{finding.get('target_repo', '?')}`: {dup.get('html_url', '?')}")

    if fix_prs:
        parts.append("")
        if len(fix_prs) == 1:
            parts.append(
                f"**Auto-fix em andamento:** {fix_prs[0][1]} — "
                "será mergeada automaticamente quando passar test + preview-smoke."
            )
        else:
            parts.append("**Auto-fixes em andamento:**")
            for finding, pr_url in fix_prs:
                parts.append(f"- `{finding.get('target_repo', '?')}`: {pr_url}")

    parts.append("")
    parts.append(
        f"_**Custo desta resolução:** ${metrics['cost_usd']:.4f} em "
        f"{metrics['calls']} chamada(s) Claude / {metrics['turns']} turn(s) / "
        f"{metrics['duration_s']:.1f}s._"
    )

    return "\n".join(parts).strip()


def _emit_metrics_summary(issue_number: int, title: str, metrics: dict) -> None:
    msg = (
        f"Issue #{issue_number}: ${metrics['cost_usd']:.4f} em "
        f"{metrics['calls']} call(s) / {metrics['turns']} turns / "
        f"{metrics['duration_s']:.1f}s — {title[:60]}"
    )
    log.info("=== RESOLUÇÃO ===  %s", msg)
    print(f"::notice title=Resolução issue #{issue_number}::{msg}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a") as f:
                f.write(
                    f"| #{issue_number} | "
                    f"${metrics['cost_usd']:.4f} | "
                    f"{metrics['calls']} | "
                    f"{metrics['turns']} | "
                    f"{metrics['duration_s']:.1f}s | "
                    f"{title[:60]} |\n"
                )
        except Exception as e:
            log.debug("Não consegui gravar GITHUB_STEP_SUMMARY: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=== Triage Pipeline (MCP + kind-aware + auto-fix + dedup + métricas) ===")
    log.info("Reports repo: %s", REPORTS_REPO)
    log.info("Repos-alvo: %s", ", ".join(TARGET_REPOS))

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a") as f:
                f.write("## Custo por resolução\n\n")
                f.write("| Issue | Custo | Calls | Turns | Tempo | Título |\n")
                f.write("|---|---|---|---|---|---|\n")
        except Exception:
            pass

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
