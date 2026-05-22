import type { OrderItem } from "../types/order.js";

const FLAT_SHIPPING = 15;
const FREE_SHIPPING_THRESHOLD = 200;

export interface TotalBreakdown {
  subtotal: number;
  shipping: number;
  total: number;
}

export function calculateTotal(items: OrderItem[]): TotalBreakdown {
  const subtotal = items.reduce(
    (acc, item) => acc + item.price * item.quantity,
    0
  );

  // Política: frete grátis para compras acima de R$ 200
  const shipping = subtotal > FREE_SHIPPING_THRESHOLD ? FLAT_SHIPPING : 0;

  const total = subtotal + shipping;

  return { subtotal, shipping, total };
}
