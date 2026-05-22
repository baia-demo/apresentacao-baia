"""
Testes unitários das funções puras de triage.py.

Cobre: classificação de actionability, agregação de labels, construção do
comentário de origem e do título da issue técnica. Não testa o pipeline
end-to-end (Claude Code / GitHub API / MCP) — só lógica pura.
"""

import os
import sys
import unittest
from pathlib import Path

# triage.py lê env no import. Preenche com valores fake antes de importar.
os.environ.setdefault("GH_PAT", "fake-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-key")
os.environ.setdefault("GITHUB_ORG", "baia-demo")
os.environ.setdefault("REPORTS_REPO", "baia-demo/user-feedback")
os.environ.setdefault("TARGET_REPOS", "catalog-api,orders-api,storefront-web")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage import (  # noqa: E402
    _aggregate_labels,
    _build_origin_comment,
    _is_actionable,
    _target_title,
)


def make_finding(**overrides) -> dict:
    base = {
        "kind": "bug",
        "confidence": 0.9,
        "target_repo": "catalog-api",
        "files_analyzed": ["src/foo.ts"],
        "summary": "Resumo",
        "explanation": "Explicação",
        "suggested_fix": "Fix",
    }
    base.update(overrides)
    return base


class TestIsActionable(unittest.TestCase):
    def test_bug_com_confianca_alta_eh_actionable(self):
        self.assertTrue(_is_actionable(make_finding(kind="bug", confidence=0.9)))

    def test_improvement_com_confianca_alta_eh_actionable(self):
        self.assertTrue(
            _is_actionable(make_finding(kind="improvement", confidence=0.8))
        )

    def test_question_nunca_eh_actionable(self):
        self.assertFalse(
            _is_actionable(
                make_finding(kind="question", target_repo=None, confidence=0.9)
            )
        )

    def test_confianca_baixa_nao_eh_actionable(self):
        self.assertFalse(
            _is_actionable(make_finding(kind="bug", confidence=0.3))
        )

    def test_sem_target_repo_nao_eh_actionable(self):
        self.assertFalse(
            _is_actionable(make_finding(kind="bug", target_repo=None))
        )


class TestAggregateLabels(unittest.TestCase):
    def test_um_bug_high_confidence_aplica_is_bug_e_repo(self):
        labels = _aggregate_labels(
            [make_finding(kind="bug", target_repo="orders-api")],
            target_issues=[(make_finding(target_repo="orders-api"), "u")],
        )
        self.assertIn("triaged", labels)
        self.assertIn("is-bug", labels)
        self.assertIn("repo:orders-api", labels)

    def test_improvement_aplica_is_improvement(self):
        labels = _aggregate_labels(
            [make_finding(kind="improvement", target_repo="storefront-web")],
            target_issues=[
                (make_finding(target_repo="storefront-web"), "u")
            ],
        )
        self.assertIn("is-improvement", labels)
        self.assertNotIn("is-bug", labels)

    def test_question_aplica_is_question_sem_repo(self):
        labels = _aggregate_labels(
            [
                make_finding(
                    kind="question",
                    target_repo=None,
                    confidence=0.9,
                )
            ],
            target_issues=[],
        )
        self.assertIn("is-question", labels)
        self.assertFalse(any(l.startswith("repo:") for l in labels))

    def test_low_confidence_sem_kind_labels(self):
        labels = _aggregate_labels(
            [make_finding(kind="bug", confidence=0.2)],
            target_issues=[],
        )
        self.assertIn("low-confidence", labels)
        self.assertNotIn("is-bug", labels)

    def test_repo_label_eh_unico_em_findings_duplicados(self):
        findings = [
            make_finding(kind="bug", target_repo="catalog-api"),
            make_finding(kind="bug", target_repo="catalog-api"),
        ]
        target_issues = [(f, f"url-{i}") for i, f in enumerate(findings)]
        labels = _aggregate_labels(findings, target_issues)
        repo_labels = [l for l in labels if l.startswith("repo:")]
        self.assertEqual(repo_labels, ["repo:catalog-api"])


class TestTargetTitle(unittest.TestCase):
    def test_bug_usa_prefixo_padrao(self):
        title = _target_title(
            "bug",
            make_finding(summary="Bug do checkout"),
            "Título original",
        )
        self.assertTrue(title.startswith("[Auto-Triage] "))
        self.assertIn("Bug do checkout", title)

    def test_improvement_usa_prefixo_de_melhoria(self):
        title = _target_title(
            "improvement",
            make_finding(summary="Adicionar botão"),
            "Título original",
        )
        self.assertTrue(title.startswith("[Auto-Triage: melhoria] "))

    def test_fallback_pra_origin_title_quando_summary_vazio(self):
        title = _target_title(
            "bug",
            make_finding(summary=""),
            "Título original",
        )
        self.assertIn("Título original", title)


def make_target_issue(url: str = "https://gh/issue/3", number: int = 3) -> dict:
    return {"html_url": url, "number": number}


class TestBuildOriginComment(unittest.TestCase):
    def test_um_bug_single_findings(self):
        comment = _build_origin_comment(
            [make_finding(kind="bug", confidence=0.92)],
            user_reply="Olá, o bug é ...",
            target_issues=[(make_finding(target_repo="catalog-api"), make_target_issue())],
            fix_prs=[],
        )
        self.assertIn("Classificado como BUG", comment)
        self.assertIn("92%", comment)
        self.assertIn("https://gh/issue/3", comment)
        self.assertIn("Olá, o bug é", comment)
        self.assertNotIn("Auto-fix", comment)

    def test_multiplos_findings_enumera(self):
        findings = [
            make_finding(kind="bug", target_repo="catalog-api", summary="A"),
            make_finding(kind="improvement", target_repo="storefront-web", summary="B"),
        ]
        comment = _build_origin_comment(
            findings,
            user_reply="resposta",
            target_issues=[
                (findings[0], make_target_issue("url1")),
                (findings[1], make_target_issue("url2")),
            ],
            fix_prs=[],
        )
        self.assertIn("2 pontos identificados", comment)
        self.assertIn("BUG: A", comment)
        self.assertIn("MELHORIA: B", comment)
        self.assertIn("Issues técnicas:", comment)

    def test_question_sem_actionable(self):
        comment = _build_origin_comment(
            [
                make_finding(
                    kind="question",
                    target_repo=None,
                    confidence=0.95,
                )
            ],
            user_reply="resposta",
            target_issues=[],
            fix_prs=[],
        )
        self.assertIn("pergunta", comment.lower())
        self.assertNotIn("Issue técnica:", comment)

    def test_inclui_pr_url_quando_fix_prs(self):
        finding = make_finding(kind="bug", target_repo="catalog-api", confidence=0.95)
        comment = _build_origin_comment(
            [finding],
            user_reply="resposta",
            target_issues=[(finding, make_target_issue())],
            fix_prs=[(finding, "https://gh/pull/7")],
        )
        self.assertIn("Auto-fix", comment)
        self.assertIn("https://gh/pull/7", comment)


class TestAutoFixEligible(unittest.TestCase):
    def test_alta_confianca_adiciona_label_auto_fix(self):
        labels = _aggregate_labels(
            [make_finding(kind="bug", confidence=0.95)],
            target_issues=[(make_finding(), {"html_url": "u"})],
        )
        self.assertIn("auto-fix-eligible", labels)

    def test_baixa_confianca_nao_adiciona_auto_fix(self):
        labels = _aggregate_labels(
            [make_finding(kind="bug", confidence=0.7)],
            target_issues=[(make_finding(), {"html_url": "u"})],
        )
        self.assertNotIn("auto-fix-eligible", labels)

    def test_question_nunca_eh_auto_fix(self):
        labels = _aggregate_labels(
            [make_finding(kind="question", target_repo=None, confidence=0.95)],
            target_issues=[],
        )
        self.assertNotIn("auto-fix-eligible", labels)


if __name__ == "__main__":
    unittest.main()
