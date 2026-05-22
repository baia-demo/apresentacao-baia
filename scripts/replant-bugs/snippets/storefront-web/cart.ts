import type { OrderItem, Product } from "./apis";

const STORAGE_KEY = "shopflow-cart";

export interface CartLine extends OrderItem {}

export function readCart(): CartLine[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as CartLine[]) : [];
  } catch {
    return [];
  }
}

export function writeCart(items: CartLine[]): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

export function addToCart(product: Product, quantity = 1): CartLine[] {
  const current = readCart();
  const existing = current.find((l) => l.productId === product.id);

  if (existing) {
    existing.quantity = quantity;
  } else {
    current.push({
      productId: product.id,
      name: product.name,
      price: product.price,
      quantity,
    });
  }

  writeCart(current);
  return current;
}

export function removeFromCart(productId: string): CartLine[] {
  const filtered = readCart().filter((l) => l.productId === productId);
  writeCart(filtered);
  return filtered;
}

export function clearCart(): void {
  writeCart([]);
}

export function cartTotal(items: CartLine[]): number {
  return items.reduce((acc, l) => acc + l.price * l.quantity, 0);
}
