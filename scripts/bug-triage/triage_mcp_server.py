"""
MCP server local com a tool `submit_triage`.

Spawnado como subprocesso do Claude Code (stdio MCP). Expõe uma única tool
estruturada que o agente chama no final da análise — garantindo que o output
final do agente seja SEMPRE um JSON válido com todos os campos corretos
(senão a tool call falha e o agente vê o erro pra corrigir).

Configurado via env vars passadas pelo orchestrator (triage.py):
- VALID_REPOS: lista comma-separated dos repos-alvo válidos
- TRIAGE_OUTPUT_FILE: caminho onde gravar o JSON do veredito
"""

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

VALID_REPOS = [
    r.strip() for r in os.environ.get("VALID_REPOS", "").split(",") if r.strip()
]
OUTPUT_FILE = Path(os.environ.get("TRIAGE_OUTPUT_FILE", "/tmp/triage_result.json"))

mcp = FastMCP("triage")


@mcp.tool()
def submit_triage(
    is_bug: bool,
    confidence: float,
    target_repo: str | None,
    files_analyzed: list[str],
    summary: str,
    explanation: str,
    suggested_fix: str | None,
    user_reply: str,
) -> str:
    """Submete o veredito final da triagem do bug. Chame UMA VEZ no fim da análise.

    Args:
        is_bug: True se for bug real, False se for uso incorreto / comportamento esperado.
        confidence: 0..1. Use < 0.5 quando estiver em dúvida (sistema marca como inconclusivo).
        target_repo: Nome do repo dono do bug (sem org). null se is_bug=False.
        files_analyzed: Caminhos dos arquivos relevantes pra análise (relativos ao repo).
        summary: Resumo curto em português (1 linha).
        explanation: Explicação técnica do bug em português.
        suggested_fix: Sugestão concreta de correção, ou null.
        user_reply: Mensagem amigável pra postar como comentário na issue original
                    (tom acessível, 2-4 linhas, pode usar markdown).
    """
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(
            f"confidence deve estar em [0, 1] — recebido: {confidence}"
        )

    if is_bug and not target_repo:
        raise ValueError("is_bug=True requer target_repo definido")

    if target_repo and VALID_REPOS and target_repo not in VALID_REPOS:
        raise ValueError(
            f"target_repo '{target_repo}' inválido. "
            f"Use um destes: {', '.join(VALID_REPOS)}"
        )

    result = {
        "is_bug": is_bug,
        "confidence": confidence,
        "target_repo": target_repo,
        "files_analyzed": files_analyzed,
        "summary": summary,
        "explanation": explanation,
        "suggested_fix": suggested_fix,
        "user_reply": user_reply,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    return (
        f"Triagem submetida ({len(result)} campos). "
        "Pode encerrar — não chame essa tool de novo."
    )


if __name__ == "__main__":
    mcp.run()
