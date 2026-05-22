"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { readCart, cartTotal, clearCart, type CartLine } from "@/lib/cart";

export function CheckoutForm() {
  const router = useRouter();
  const [items, setItems] = useState<CartLine[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setItems(readCart());
  }, []);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    const form = new FormData(e.currentTarget);
    const customerId = String(form.get("customerId") ?? "").trim();
    const email = String(form.get("email") ?? "").trim();

    if (!customerId || !email) {
      setError("Preencha todos os campos");
      setSubmitting(false);
      return;
    }

    try {
      const res = await fetch("/api/orders", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ customerId, items }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const order = await res.json();
      clearCart();
      router.push(`/checkout/success?id=${order.id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao processar pedido");
      setSubmitting(false);
    }
  }

  if (items.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        Carrinho vazio. Adicione produtos antes do checkout.
      </p>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div className="space-y-4 rounded border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Resumo</h2>
        <ul className="divide-y divide-slate-100 text-sm">
          {items.map((line) => (
            <li
              key={line.productId}
              className="flex items-center justify-between py-2"
            >
              <span>
                {line.quantity}x {line.name}
              </span>
              <span>
                R${" "}
                {(line.price * line.quantity).toFixed(2).replace(".", ",")}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex items-center justify-between border-t border-slate-200 pt-3 font-semibold">
          <span>Subtotal</span>
          <span>R$ {cartTotal(items).toFixed(2).replace(".", ",")}</span>
        </div>
      </div>

      <div className="space-y-3 rounded border border-slate-200 bg-white p-4">
        <h2 className="font-semibold">Dados do cliente</h2>

        <label className="block text-sm">
          <span className="mb-1 block text-slate-700">ID do cliente</span>
          <input
            name="customerId"
            type="text"
            required
            defaultValue="demo-customer"
            className="w-full rounded border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm">
          <span className="mb-1 block text-slate-700">E-mail</span>
          <input
            name="email"
            type="email"
            required
            defaultValue="demo@shopflow.dev"
            className="w-full rounded border border-slate-300 px-3 py-2"
          />
        </label>
      </div>

      {error && (
        <div className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </div>
      )}

      <button
        type="submit"
        className="w-full rounded bg-brand px-4 py-3 text-base font-semibold text-white hover:bg-brand-dark"
      >
        {submitting ? "Processando..." : "Finalizar compra"}
      </button>
    </form>
  );
}
