<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Model;

use Magento\Framework\App\ResourceConnection;
use Magento\Framework\Model\CallbackPool;
use Psr\Log\LoggerInterface;

/**
 * Entry point of the observers: records one change notification, safely.
 *
 * Never blocks or breaks the save that triggered it:
 * - inside a transaction (checkout, admin save) the outbox row is written by a commit
 *   callback, i.e. after the customer's transaction committed and outside its locks; a
 *   rolled-back save records nothing (Magento drops the callbacks on rollback);
 * - nothing here talks to Odoo; the cron sends the outbox;
 * - every error is caught and logged.
 */
class Recorder
{
    /**
     * @param Config $config
     * @param Outbox $outbox
     * @param ResourceConnection $resource
     * @param LoggerInterface $logger
     */
    public function __construct(
        private readonly Config $config,
        private readonly Outbox $outbox,
        private readonly ResourceConnection $resource,
        private readonly LoggerInterface $logger
    ) {
    }

    /**
     * Record that a record changed.
     *
     * @param string $entityType one of the Outbox::ENTITY_* constants
     * @param int|string|null $entityId Magento entity id
     * @param string|null $entityRef SKU (products, stock) or increment id (sales documents)
     * @param string $eventType created, updated, status_changed or deleted
     * @param int|string|null $orderId parent order id of an invoice, shipment or credit memo
     * @param int[] $returnToStockItems credit memos: order item ids returned to stock
     * @return void
     */
    public function record(
        string $entityType,
        int|string|null $entityId,
        ?string $entityRef,
        string $eventType,
        int|string|null $orderId = null,
        array $returnToStockItems = []
    ): void {
        try {
            if (!$this->config->isEventEnabled($entityType)) {
                return;
            }
            $entityId = $entityId === null || $entityId === '' ? null : (string)$entityId;
            $orderId = $orderId === null || $orderId === '' ? null : (string)$orderId;
            $returned = $returnToStockItems ? implode(',', array_map('intval', $returnToStockItems)) : null;
            $write = function () use ($entityType, $entityId, $entityRef, $eventType, $orderId, $returned): void {
                try {
                    $ref = $entityRef;
                    if ($entityType === Outbox::ENTITY_STOCK && ($ref === null || $ref === '') && $entityId) {
                        $ref = $this->outbox->skuOf((int)$entityId);
                    }
                    if ($entityType === Outbox::ENTITY_STOCK) {
                        if ($ref === null || $ref === '') {
                            return;
                        }
                        $entityId = null; // one row per SKU, whichever save reported it
                    }
                    $this->outbox->add($entityType, $entityId, $ref, $eventType, $orderId, $returned);
                } catch (\Throwable $e) {
                    $this->logger->error('SDLC_OdooConnector: could not record event: ' . $e->getMessage());
                }
            };
            $connection = $this->resource->getConnection();
            if ($connection->getTransactionLevel() > 0) {
                CallbackPool::attach(spl_object_hash($connection), $write);
            } else {
                $write();
            }
        } catch (\Throwable $e) {
            $this->logger->error('SDLC_OdooConnector: could not record event: ' . $e->getMessage());
        }
    }
}
