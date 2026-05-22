# Slides — BaIA Talk

Slides da palestra "Triagem Autônoma de Bugs com Claude Code Headless".

## Setup

```bash
cd slides
npm install
```

## Rodar local (apresentação ao vivo / edição)

```bash
npm run dev
```

Abre `http://localhost:3030`. Hot reload em qualquer mudança de `slides.md`.

**Atalhos durante apresentação:**
- `f` — fullscreen
- `o` — overview
- `s` — speaker mode (notas em outra janela)
- `d` — dark mode toggle
- `→ / ←` — navegar slides

## Export

```bash
npm run export       # gera dist/slides-export.pdf
npm run export-png   # uma PNG por slide (pra usar em outras ferramentas)
```

## Estrutura

- `slides.md` — todo o deck (separadores `---` entre slides)
- `public/` — assets (screenshots, logos, fluxogramas)
- `components/` — componentes Vue custom (se precisar)
- `style.css` — overrides de CSS

## Como adicionar screenshots

1. Tira screenshot e salva em `public/screenshots/`
2. No slide: `![](/screenshots/nome.png)` ou `<img src="/screenshots/nome.png" />`

## Theme

Default é `seriph` (editorial, bom pra dev talk com mix de texto + código).
Pra trocar pro `default` (mais "deck-like"), muda a primeira linha do
`slides.md`: `theme: default`.
