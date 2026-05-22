#!/usr/bin/env bash
# Replanta os bugs da demo nos 3 repos da ShopFlow.
#
# Uso (de qualquer lugar):
#   bash scripts/replant-bugs/replant.sh [--dry-run]
#
# O que faz:
#   - Clona (ou usa clone existente em /tmp/replant-XXX) os 3 repos
#   - Sobrescreve arquivos com a versão BUGADA canônica
#   - Se houve diff, commita + push (ci.yml redeploya automaticamente)
#
# Idempotente: se a versão atual já é a bugada, não faz nada.
#
# Requer:
#   - gh CLI autenticado
#   - git
#
# Bugs replantados:
#   catalog-api:
#     - searchService.ts: tokenize sem normalize() (busca sem acento falha)
#     - searchService.test.ts: REMOVE o teste de busca-sem-acento que o
#       auto-fix adiciona (volta pra versão original com gaps)
#   orders-api:
#     - totalCalculator.ts: condição de frete grátis INVERTIDA
#     - routes/orders.ts: GET /orders retorna total=0 na projeção
#   storefront-web:
#     - lib/cart.ts: addToCart usa = em vez de += (não soma quantidade);
#       removeFromCart usa === em vez de !== (remove o errado)
#     - components/CheckoutForm.tsx: botão "Finalizar compra" sem `disabled`

set -e

DRY_RUN=0
[[ "$1" == "--dry-run" ]] && DRY_RUN=1

WORK_DIR=$(mktemp -d -t replant-XXXXXX)
trap "rm -rf $WORK_DIR" EXIT

ORG="baia-demo"
SNIPPETS_DIR="$(cd "$(dirname "$0")" && pwd)/snippets"

run_or_show() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] $*"
  else
    "$@"
  fi
}

replant_repo() {
  local repo="$1"
  shift
  local files=("$@")

  echo ""
  echo "═══ $repo ═══"
  cd "$WORK_DIR"
  gh repo clone "$ORG/$repo" "$repo" -- --depth 1 >/dev/null 2>&1
  cd "$repo"

  local changed=0
  for entry in "${files[@]}"; do
    local snippet="${entry%%:*}"
    local target="${entry#*:}"
    local src="$SNIPPETS_DIR/$repo/$snippet"

    if [ ! -f "$src" ]; then
      echo "  ⚠️  snippet faltando: $src"
      continue
    fi

    mkdir -p "$(dirname "$target")"
    if [ -f "$target" ] && cmp -s "$src" "$target"; then
      echo "  ✓ $target (já bugado, sem diff)"
    else
      cp "$src" "$target"
      echo "  ✚ $target (sobrescrito com versão bugada)"
      changed=1
    fi
  done

  if [ "$changed" -eq 0 ]; then
    echo "  → sem mudanças, skip commit"
    return
  fi

  git diff --stat
  echo ""
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  [dry-run] commit + push"
    return
  fi

  git config user.email "replant-script@baia-demo.dev"
  git config user.name "Replant Script"
  git add -A
  git commit -m "chore: replant bugs (demo BaIA)

Restaura os bugs plantados pra próxima demo. CI deploya automaticamente."
  git push
  echo "  ✓ pushed"
}

echo "═══ Replant Bugs — ShopFlow / BaIA demo ═══"
if [ "$DRY_RUN" -eq 1 ]; then echo "(dry-run — nada será commitado)"; fi

replant_repo "catalog-api" \
  "searchService.ts:src/services/searchService.ts" \
  "searchService.test.ts:src/services/searchService.test.ts"

replant_repo "orders-api" \
  "totalCalculator.ts:src/services/totalCalculator.ts" \
  "routes-orders.ts:src/routes/orders.ts"

replant_repo "storefront-web" \
  "cart.ts:lib/cart.ts" \
  "CheckoutForm.tsx:components/CheckoutForm.tsx"

echo ""
echo "═══ Pronto. CI vai deployar (~3-5min). ═══"
