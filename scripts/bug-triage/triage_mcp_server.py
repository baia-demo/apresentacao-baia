"""
MCP server local com a tool `submit_triage`.

Spawnado como subprocesso do Claude Code (stdio MCP). Expõe uma única tool
que aceita uma LISTA de findings — porque um report do usuário pode citar
1+ bugs distintos, e cada um gera uma issue técnica separada no repo certo.

Configurado via env vars passadas pelo orchestrator (triage.py):
- VALID_REPOS: lista comma-separated dos repos-alvo válidos
- TRIAGE_OUTPUT_FILE: caminho onde gravar o JSON do veredito
"""

import json
import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

VALID_REPOS = [
    r.strip() for r in os.environ.get("VALID_REPOS", "").split(",") if r.strip()
]
OUTPUT_FILE = Path(os.environ.get("TRIAGE_OUTPUT_FILE", "/tmp/triage_result.json"))

mcp = FastMCP("triage")


class BugFinding(BaseModel):
    """Um bug específico identificado dentro de um report.

    Para reports com 1 bug, use uma lista de 1 elemento.
    Para reports com múltiplos bugs distintos (ex: 'busca não funciona E
    o total tá errado'), use 1 elemento por bug.
    """

    is_bug: bool = Field(
        description="True se for bug real; False se uso incorreto/comportamento esperado."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confiança 0..1. Use < 0.5 quando estiver em dúvida.",
    )
    target_repo: Optional[str] = Field(
        description="Nome do repo dono (sem org). null se is_bug=False."
    )
    files_analyzed: list[str] = Field(
        default_factory=list,
        description="Paths relativos ao repo dos arquivos relevantes.",
    )
    summary: str = Field(description="Resumo curto em português (1 linha).")
    explanation: str = Field(description="Explicação técnica em português.")
    suggested_fix: Optional[str] = Field(
        description="Sugestão concreta de correção, ou null."
    )


@mcp.tool()
def submit_triage(
    findings: list[BugFinding],
    user_reply: str,
) -> str:
    """Submete o veredito final da triagem. Chame UMA VEZ no fim da análise.

    Args:
        findings: Lista de bugs identificados.
            - Report com 1 bug: lista de 1 elemento.
            - Report com N bugs distintos: lista de N elementos.
            - Report que não é bug: lista de 1 elemento com is_bug=False.
            - Não consegue analisar: lista de 1 elemento com is_bug=False,
              confidence=0.0, target_repo=None, summary explicando.
        user_reply: Comentário único pra postar na issue original do reportador.
            Quando houver múltiplos findings, mencione todos resumidamente.
            Tom acessível pra não-engenheiro, 2-5 linhas, pode usar markdown.
    """
    if not findings:
        raise ValueError(
            "findings não pode ser vazia. Use ao menos 1 elemento "
            "(com is_bug=False se a análise foi inconclusiva)."
        )

    for i, f in enumerate(findings):
        if f.is_bug and not f.target_repo:
            raise ValueError(
                f"findings[{i}]: is_bug=True requer target_repo definido."
            )
        if f.target_repo and VALID_REPOS and f.target_repo not in VALID_REPOS:
            raise ValueError(
                f"findings[{i}]: target_repo '{f.target_repo}' inválido. "
                f"Use um destes: {', '.join(VALID_REPOS)}"
            )

    result = {
        "findings": [f.model_dump() for f in findings],
        "user_reply": user_reply,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    return (
        f"Triagem submetida ({len(findings)} finding(s)). "
        "Pode encerrar — não chame essa tool de novo."
    )


if __name__ == "__main__":
    mcp.run()
