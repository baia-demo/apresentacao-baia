import { describe, test, expect } from "vitest";
import { searchProducts } from "./searchService.js";

describe("searchProducts", () => {
  test("retorna todos os produtos com query vazia", () => {
    const results = searchProducts("");
    expect(results.length).toBeGreaterThan(0);
  });

  test("encontra produto pelo nome quando termo bate por prefixo", () => {
    const results = searchProducts("camiseta");
    expect(results.length).toBeGreaterThan(0);
    expect(
      results.some((p) => p.name.toLowerCase().includes("camiseta"))
    ).toBe(true);
  });

  test("encontra produto quando termo já contém o acento correto", () => {
    const results = searchProducts("tênis");
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].name.toLowerCase()).toContain("tênis");
  });

  test("filtra por categoria sem termo", () => {
    const results = searchProducts("", "vestuario");
    expect(results.length).toBeGreaterThan(0);
    expect(results.every((p) => p.category === "vestuario")).toBe(true);
  });

  test("combina categoria + termo", () => {
    const results = searchProducts("camiseta", "vestuario");
    expect(
      results.every(
        (p) =>
          p.category === "vestuario" &&
          p.name.toLowerCase().includes("camiseta")
      )
    ).toBe(true);
  });

  test("retorna lista vazia quando nada bate", () => {
    const results = searchProducts("xyz-produto-inexistente");
    expect(results).toHaveLength(0);
  });
});
