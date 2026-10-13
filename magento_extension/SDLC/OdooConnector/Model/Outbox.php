<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Model;

use Magento\Framework\App\ResourceConnection;
use Magento\Framework\DataObject\IdentityGeneratorInterface;
use Magento\Framework\DB\Adapter\AdapterInterface;

/**
 * Data access of the `sdlc_odoo_event_outbox` table.
 *
 * Life cycle of a row: pending -> processing (claimed by one sender run) -> sent, or
 * retry (exponential backoff) -> ... -> failed after the configured number of attempts.
 * While a row is pending its `dedupe_key` is set, so repeated saves of one record
 * update that row instead of adding new ones; claiming clears the key.
 */
class Outbox
{
    public const TABLE = 'sdlc_odoo_event_outbox';

    public const ENTITY_ORDER = 'order';
    public const ENTITY_INVOICE = 'invoice';
    public const ENTITY_SHIPMENT = 'shipment';
    public const ENTITY_CREDITMEMO = 'creditmemo';
    public const ENTITY_PRODUCT = 'product';
    public const ENTITY_CUSTOMER = 'customer';
    public const ENTITY_STOCK = 'stock';

    public const STATUS_PENDING = 'pending';
    public const STATUS_PROCESSING = 'processing';
    public const STATUS_RETRY = 'retry';
    public const STATUS_SENT = 'sent';
    public const STATUS_FAILED = 'failed';

    /** A claimed batch whose sender died is released after this many seconds. */
    private const STALE_CLAIM_SECONDS = 600;
    private const BACKOFF_BASE_SECONDS = 60;
    private const BACKOFF_MAX_SECONDS = 3600;
    private const FAILED_RETENTION_DAYS = 30;

    /**
     * @param ResourceConnection $resource
     * @param IdentityGeneratorInterface $identityGenerator
     */
    public function __construct(
        private readonly ResourceConnection $resource,
        private readonly IdentityGeneratorInterface $identityGenerator
    ) {
    }

    /**
     * Add (or coalesce into the pending row of the same record) one event.
     *
     * @param string $entityType
     * @param string|null $entityId
     * @param string|null $entityRef
     * @param string $eventType
     * @param string|null $orderId
     * @param string|null $returnToStockItems comma-separated order item ids (credit memos)
     * @return void
     */
    public function add(
        string $entityType,
        ?string $entityId,
        ?string $entityRef,
        string $eventType,
        ?string $orderId = null,
        ?string $returnToStockItems = null
    ): void {
        $now = $this->now();
        $row = [
            'event_uuid' => $this->identityGenerator->generateId(),
            'entity_type' => $entityType,
            'entity_id' => $entityId,
            'entity_ref' => $entityRef !== null ? mb_substr($entityRef, 0, 128) : null,
            'order_id' => $orderId,
            'return_to_stock_items' => $returnToStockItems !== null ? mb_substr($returnToStockItems, 0, 255) : null,
            'event_type' => $eventType,
            'dedupe_key' => mb_substr($entityType . ':' . ($entityId ?? '') . ':' . ($entityRef ?? ''), 0, 255),
            'status' => self::STATUS_PENDING,
            'attempts' => 0,
            'created_at' => $now,
            'updated_at' => $now,
        ];
        $this->connection()->insertOnDuplicate(
            $this->table(),
            $row,
            ['event_uuid', 'event_type', 'entity_ref', 'order_id', 'return_to_stock_items', 'updated_at']
        );
    }

    /**
     * Atomically claim up to $limit due rows for one delivery; returns them oldest first.
     *
     * @param int $limit
     * @return array<int, array<string, mixed>>
     */
    public function claim(int $limit): array
    {
        $connection = $this->connection();
        $token = bin2hex(random_bytes(16));
        $now = $this->now();
        $connection->query(
            sprintf(
                'UPDATE %s SET status = ?, claim_token = ?, dedupe_key = NULL, updated_at = ?'
                . ' WHERE status IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)'
                . ' ORDER BY outbox_id LIMIT %d',
                $connection->quoteIdentifier($this->table()),
                max(1, $limit)
            ),
            [self::STATUS_PROCESSING, $token, $now, self::STATUS_PENDING, self::STATUS_RETRY, $now]
        );
        $select = $connection->select()
            ->from($this->table())
            ->where('claim_token = ?', $token)
            ->order('outbox_id ASC');
        return $connection->fetchAll($select);
    }

    /**
     * @param int[] $ids
     * @param int $httpStatus
     * @return void
     */
    public function markSent(array $ids, int $httpStatus): void
    {
        if (!$ids) {
            return;
        }
        $now = $this->now();
        $this->connection()->update(
            $this->table(),
            [
                'status' => self::STATUS_SENT,
                'sent_at' => $now,
                'updated_at' => $now,
                'claim_token' => null,
                'last_http_status' => $httpStatus,
                'last_error' => null,
                'attempts' => new \Zend_Db_Expr('attempts + 1'),
            ],
            ['outbox_id IN (?)' => array_map('intval', $ids)]
        );
    }

    /**
     * Failed delivery: retry later with backoff, or give up after $maxAttempts.
     *
     * @param array<int, array<string, mixed>> $rows
     * @param int $maxAttempts
     * @param int|null $httpStatus
     * @param string $error
     * @return int number of rows given up
     */
    public function markFailed(array $rows, int $maxAttempts, ?int $httpStatus, string $error): int
    {
        $connection = $this->connection();
        $now = time();
        $givenUp = 0;
        foreach ($rows as $row) {
            $attempts = (int)$row['attempts'] + 1;
            $failed = $attempts >= $maxAttempts;
            $givenUp += (int)$failed;
            $connection->update(
                $this->table(),
                [
                    'status' => $failed ? self::STATUS_FAILED : self::STATUS_RETRY,
                    'attempts' => $attempts,
                    'next_attempt_at' => $failed ? null : gmdate('Y-m-d H:i:s', $now + $this->backoff($attempts)),
                    'claim_token' => null,
                    'last_http_status' => $httpStatus,
                    'last_error' => mb_substr($error, 0, 255),
                    'updated_at' => gmdate('Y-m-d H:i:s', $now),
                ],
                ['outbox_id = ?' => (int)$row['outbox_id']]
            );
        }
        return $givenUp;
    }

    /**
     * Seconds to wait after the n-th failed attempt: 1, 2, 4 ... 60 minutes.
     *
     * @param int $attempts
     * @return int
     */
    public function backoff(int $attempts): int
    {
        return (int)min(self::BACKOFF_BASE_SECONDS * (2 ** max(0, $attempts - 1)), self::BACKOFF_MAX_SECONDS);
    }

    /**
     * Release batches claimed by a sender that died.
     *
     * @return int
     */
    public function releaseStale(): int
    {
        return $this->connection()->update(
            $this->table(),
            ['status' => self::STATUS_RETRY, 'claim_token' => null],
            [
                'status = ?' => self::STATUS_PROCESSING,
                'updated_at < ?' => gmdate('Y-m-d H:i:s', time() - self::STALE_CLAIM_SECONDS),
            ]
        );
    }

    /**
     * Delete delivered rows older than $retentionDays and failed rows older than 30 days.
     *
     * @param int $retentionDays
     * @return int
     */
    public function cleanup(int $retentionDays): int
    {
        $connection = $this->connection();
        $deleted = $connection->delete($this->table(), [
            'status = ?' => self::STATUS_SENT,
            'sent_at < ?' => gmdate('Y-m-d H:i:s', time() - $retentionDays * 86400),
        ]);
        $deleted += $connection->delete($this->table(), [
            'status = ?' => self::STATUS_FAILED,
            'updated_at < ?' => gmdate('Y-m-d H:i:s', time() - max($retentionDays, self::FAILED_RETENTION_DAYS) * 86400),
        ]);
        return $deleted;
    }

    /**
     * Make failed and waiting rows due now (attempts reset).
     *
     * @return int
     */
    public function retryFailed(): int
    {
        return $this->connection()->update(
            $this->table(),
            ['status' => self::STATUS_RETRY, 'attempts' => 0, 'next_attempt_at' => null],
            ['status IN (?)' => [self::STATUS_FAILED, self::STATUS_RETRY]]
        );
    }

    /**
     * @return array<string, int> status => rows
     */
    public function countByStatus(): array
    {
        $connection = $this->connection();
        $select = $connection->select()
            ->from($this->table(), ['status', 'n' => new \Zend_Db_Expr('COUNT(*)')])
            ->group('status');
        return array_map('intval', $connection->fetchPairs($select));
    }

    /**
     * Latest rows that are waiting for a retry or were given up.
     *
     * @param int $limit
     * @return array<int, array<string, mixed>>
     */
    public function latestProblems(int $limit = 10): array
    {
        $connection = $this->connection();
        $select = $connection->select()
            ->from($this->table(), [
                'outbox_id', 'entity_type', 'entity_id', 'entity_ref', 'status', 'attempts',
                'next_attempt_at', 'last_http_status', 'last_error',
            ])
            ->where('status IN (?)', [self::STATUS_RETRY, self::STATUS_FAILED])
            ->order('outbox_id DESC')
            ->limit($limit);
        return $connection->fetchAll($select);
    }

    /**
     * SKU of a product id (stock items do not carry it).
     *
     * @param int $productId
     * @return string|null
     */
    public function skuOf(int $productId): ?string
    {
        $connection = $this->connection();
        $sku = $connection->fetchOne(
            $connection->select()
                ->from($this->resource->getTableName('catalog_product_entity'), ['sku'])
                ->where('entity_id = ?', $productId)
        );
        return $sku === false || $sku === null || $sku === '' ? null : (string)$sku;
    }

    /**
     * @return AdapterInterface
     */
    public function connection(): AdapterInterface
    {
        return $this->resource->getConnection();
    }

    /**
     * @return string
     */
    private function table(): string
    {
        return $this->resource->getTableName(self::TABLE);
    }

    /**
     * Current UTC time in DB format (all outbox times are UTC, written by PHP).
     *
     * @return string
     */
    private function now(): string
    {
        return gmdate('Y-m-d H:i:s');
    }
}
