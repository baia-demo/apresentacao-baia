"""
MCP server local com a tool `submit_triage`.

Spawnado como subprocesso do Claude Code (stdio MCP). Expõe uma única tool
que aceita uma LISTA de findings. Cada finding tem um `kind` que classifica
o report (bug / improvement / question / unclear) — porque "is_bug" não
cobre o caso real onde o usuário manda sugestão ou dúvida e o agente acaba
forçando como bug (confirmation bias).

Configurado via env vars:
- VALID_REPOS: lista comma-separated dos repos-alvo válidos
- TRIAGE_OUTPUT_FILE: caminho onde gravar o JSON do veredito
"""

import json
import os
from pathlib import Path
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

VALID_REPOS = [
    r.strip() for r in os.environ.get("VALID_REPOS", "").split(",") if r.strip()
]
OUTPUT_FILE = Path(os.environ.get("TRIAGE_OUTPUT_FILE", "/tmp/triage_result.json"))

FindingKind = Literal["bug", "improvement", "question", "unclear"]

mcp = FastMCP("triage")


class Finding(BaseModel):
    """Um ponto distinto identificado num report. Pode ser bug, sugestão de
    melhoria, dúvida ou item não-classificável.

    Para reports com 1 ponto, use lista de 1 elemento.
    Para reports compostos ("o X tá quebrado E o Y poderia ter Z"), use 1
    elemento por ponto.
    """

    kind: FindingKind = Field(
        description=(
            "Tipo do report: "
            "'bug' (defeito real no código); "
            "'improvement' (sugestão de feature/UX, comportamento atual está OK); "
            "'question' (usuário fazendo pergunta, sem reportar problema); "
            "'unclear' (não dá pra entender o que ele quer dizer)."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confiança 0..1. < 0.5 = inconclusivo (não cria issue técnica).",
    )
    target_repo: Optional[str] = Field(
        description=(
            "Repo onde a correção/feature mora. "
            "Obrigatório se kind='bug' ou 'improvement'. "
            "null se kind='question' ou 'unclear'."
        )
    )
    files_analyzed: list[str] = Field(
        default_factory=list,
        description="Paths relativos ao repo (vazio se não investigou código).",
    )
    summary: str = Field(description="Resumo curto em português (1 linha).")
    explanation: str = Field(description="Explicação técnica em português.")
    suggested_fix: Optional[str] = Field(
        description="Sugestão concreta de fix (bug) ou de implementação (improvement). null se question/unclear."
    )


@mcp.tool()
def submit_triage(
    findings: list[Finding],
    user_reply: str,
) -> str:
    """Submete o veredito final da triagem. Chame UMA VEZ no fim da análise.

    Cada `finding` na lista representa um ponto distinto identificado no
    report do usuário. Para a maioria dos reports (1 ponto), use lista de
    1 elemento.

    Args:
        findings: Lista de pontos identificados.
            - Report claro de 1 bug → 1 finding kind='bug'.
            - Sugestão de melhoria → 1 finding kind='improvement'.
            - Report misto ("X quebrado E Y poderia melhorar") → 2 findings.
            - Pergunta → kind='question'.
            - Não dá pra entender → kind='unclear', confidence=0.0.
        user_reply: Comentário ÚNICO consolidado pra postar como resposta na
            issue original do usuário. Quando houver múltiplos findings,
            cubra todos resumidamente. Tom acessível, 2-5 linhas, markdown OK.
    """
    if not findings:
        raise ValueError(
            "findings não pode ser vazia. Use pelo menos 1 elemento "
            "(com kind='unclear' se a análise foi inconclusiva)."
        )

    for i, f in enumerate(findings):
        if f.kind in ("bug", "improvement") and not f.target_repo:
            raise ValueError(
                f"findings[{i}]: kind='{f.kind}' requer target_repo definido."
            )
        if (
            f.kind in ("question", "unclear")
            and f.target_repo
        ):
            raise ValueError(
                f"findings[{i}]: kind='{f.kind}' não deve ter target_repo. "
                "Esses tipos não geram issue técnica."
            )
        if f.target_repo and VALID_REPOS and f.target_repo not in VALID_REPOS:
            raise ValueError(
                f"findings[{i}]: target_repo '{f.target_repo}' inválido. "
                f"Use: {', '.join(VALID_REPOS)}"
            )

    result = {
        "findings": [f.model_dump() for f in findings],
        "user_reply": user_reply,
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    kinds_summary = ", ".join(f.kind for f in findings)
    return (
        f"Triagem submetida ({len(findings)} finding(s): {kinds_summary}). "
        "Pode encerrar — não chame essa tool de novo."
    )


if __name__ == "__main__":
    mcp.run()
