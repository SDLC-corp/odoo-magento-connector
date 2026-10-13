<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Model;

/**
 * Holds the `return_to_stock_items` of the refund being executed (REST/service refunds pass
 * them as creation arguments, not on the credit memo items), so the credit memo observer
 * can report which order items went back to stock. Shared instance, one refund at a time.
 */
class ReturnToStockContext
{
    /** @var int[]|null */
    private ?array $orderItemIds = null;

    /**
     * @param int[]|null $orderItemIds
     * @return void
     */
    public function set(?array $orderItemIds): void
    {
        $this->orderItemIds = $orderItemIds === null ? null : array_values(array_unique(array_map('intval', $orderItemIds)));
    }

    /**
     * @return int[]
     */
    public function get(): array
    {
        return $this->orderItemIds ?? [];
    }

    /**
     * @return void
     */
    public function clear(): void
    {
        $this->orderItemIds = null;
    }
}
