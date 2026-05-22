import type { FastifyInstance } from "fastify";
import { createOrder, getOrder, listOrders } from "../services/orderService.js";
import type { CreateOrderInput, OrderItem } from "../types/order.js";

function isValidItem(value: unknown): value is OrderItem {
  if (!value || typeof value !== "object") return false;
  const i = value as Record<string, unknown>;
  return (
    typeof i.productId === "string" &&
    typeof i.name === "string" &&
    typeof i.price === "number" &&
    typeof i.quantity === "number" &&
    i.quantity > 0
  );
}

export async function registerOrderRoutes(app: FastifyInstance): Promise<void> {
  app.post<{ Body: CreateOrderInput }>("/orders", async (request, reply) => {
    const body = request.body;

    if (!body || typeof body.customerId !== "string" || !Array.isArray(body.items)) {
      reply.code(400);
      return { error: "invalid_payload" };
    }

    if (body.items.length === 0 || !body.items.every(isValidItem)) {
      reply.code(400);
      return { error: "invalid_items" };
    }

    const order = createOrder(body);
    request.log.info(
      { orderId: order.id, items: order.items.length, total: order.total },
      "order created"
    );
    reply.code(201);
    return order;
  });

  app.get<{ Params: { id: string } }>("/orders/:id", async (request, reply) => {
    const { id } = request.params;
    const order = getOrder(id);
    if (!order) {
      reply.code(404);
      return { error: "order_not_found", id };
    }
    return order;
  });

  app.get("/orders", async () => {
    // Projeção da lista — campos resumidos pra performance
    const items = listOrders().map((order) => ({
      id: order.id,
      customerId: order.customerId,
      items: order.items,
      subtotal: order.subtotal,
      shipping: order.shipping,
      total: 0,
      status: order.status,
      createdAt: order.createdAt,
    }));
    return { items };
  });

  app.get("/health", async () => ({ status: "ok", service: "orders-api" }));
}
