<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Model;

use Magento\Framework\DataObject\IdentityGeneratorInterface;
use Magento\Framework\HTTP\Client\Curl;
use Magento\Framework\HTTP\Client\CurlFactory;
use Psr\Log\LoggerInterface;

/**
 * Pushes the outbox to Odoo.
 *
 * Request: POST <Odoo Webhook URL>, JSON body
 * {"delivery_id": "...", "timestamp": <unix>, "source": "magento", "events": [...]}
 * with headers X-SDLC-Signature: sha256=<hex HMAC-SHA256(body, secret)>,
 * X-SDLC-Timestamp: <unix> and X-SDLC-Event-Id: <delivery id>. The body repeats the
 * timestamp and delivery id so the signature covers them. Events carry ids only.
 */
class Sender
{
    public const USER_AGENT = 'SDLC-OdooConnector/1.0';

    /** Stop starting new batches after this many seconds (one cron minute). */
    private const TIME_BUDGET_SECONDS = 45;

    /**
     * @param Config $config
     * @param Outbox $outbox
     * @param CurlFactory $curlFactory
     * @param IdentityGeneratorInterface $identityGenerator
     * @param LoggerInterface $logger
     */
    public function __construct(
        private readonly Config $config,
        private readonly Outbox $outbox,
        private readonly CurlFactory $curlFactory,
        private readonly IdentityGeneratorInterface $identityGenerator,
        private readonly LoggerInterface $logger
    ) {
    }

    /**
     * Send due outbox rows in batches until the outbox is empty, a delivery fails, or
     * the time budget is spent.
     *
     * @param int $maxBatches
     * @return array<string, mixed> run statistics
     */
    public function run(int $maxBatches = 20): array
    {
        $stats = ['batches' => 0, 'rows_sent' => 0, 'events_sent' => 0, 'rows_failed' => 0,
            'rows_given_up' => 0, 'last_status' => null, 'last_error' => null, 'skipped' => null];
        if (!$this->config->isEnabled()) {
            $stats['skipped'] = 'module disabled or no Odoo URL';
            return $stats;
        }
        $secret = $this->config->getSecret();
        if ($secret === '') {
            $stats['skipped'] = 'no shared secret';
            return $stats;
        }
        $this->outbox->releaseStale();
        $started = microtime(true);
        while ($stats['batches'] < $maxBatches && microtime(true) - $started < self::TIME_BUDGET_SECONDS) {
            $rows = $this->outbox->claim($this->config->getBatchSize());
            if (!$rows) {
                break;
            }
            $stats['batches']++;
            $events = $this->buildEvents($rows);
            $result = $this->post($events, $secret);
            $stats['last_status'] = $result['status'];
            if ($result['ok']) {
                $this->outbox->markSent(array_column($rows, 'outbox_id'), (int)$result['status']);
                $stats['rows_sent'] += count($rows);
                $stats['events_sent'] += count($events);
                continue;
            }
            $stats['last_error'] = $result['error'];
            $stats['rows_failed'] += count($rows);
            $stats['rows_given_up'] += $this->outbox->markFailed(
                $rows,
                $this->config->getMaxAttempts(),
                $result['status'],
                $result['error']
            );
            $this->logger->warning('SDLC_OdooConnector: delivery to Odoo failed: ' . $result['error']);
            break; // Odoo is down or rejecting: do not hammer it, the backoff decides
        }
        return $stats;
    }

    /**
     * One event per record: rows of the same record in the batch are coalesced (newest wins).
     *
     * @param array<int, array<string, mixed>> $rows
     * @return array<int, array<string, mixed>>
     */
    public function buildEvents(array $rows): array
    {
        $byRecord = [];
        foreach ($rows as $row) {
            $key = $row['entity_type'] . ':' . ($row['entity_id'] ?? '') . ':' . ($row['entity_ref'] ?? '');
            $byRecord[$key] = $row; // rows are ordered by outbox_id: the newest one stays
        }
        $events = [];
        foreach ($byRecord as $row) {
            $event = [
                'id' => (string)$row['event_uuid'],
                'entity' => (string)$row['entity_type'],
                'event' => (string)$row['event_type'],
                'occurred_at' => str_replace(' ', 'T', (string)$row['updated_at']) . 'Z',
            ];
            if ($row['entity_id'] !== null && $row['entity_id'] !== '') {
                $event['entity_id'] = (string)$row['entity_id'];
            }
            switch ($row['entity_type']) {
                case Outbox::ENTITY_PRODUCT:
                case Outbox::ENTITY_STOCK:
                    $event['sku'] = (string)$row['entity_ref'];
                    break;
                case Outbox::ENTITY_ORDER:
                    $event['increment_id'] = (string)$row['entity_ref'];
                    break;
                case Outbox::ENTITY_INVOICE:
                case Outbox::ENTITY_SHIPMENT:
                case Outbox::ENTITY_CREDITMEMO:
                    $event['order_id'] = (string)$row['order_id'];
                    $event['increment_id'] = (string)$row['entity_ref'];
                    if ($row['entity_type'] === Outbox::ENTITY_CREDITMEMO) {
                        $event['return_to_stock_items'] = array_values(array_filter(array_map(
                            'intval',
                            explode(',', (string)($row['return_to_stock_items'] ?? ''))
                        )));
                    }
                    break;
                default:
                    break;
            }
            $events[] = $event;
        }
        return $events;
    }

    /**
     * Sign and POST one batch.
     *
     * @param array<int, array<string, mixed>> $events
     * @param string $secret
     * @return array{ok: bool, status: int|null, error: string}
     */
    private function post(array $events, string $secret): array
    {
        $deliveryId = $this->identityGenerator->generateId();
        $timestamp = time();
        $body = json_encode(
            ['delivery_id' => $deliveryId, 'timestamp' => $timestamp, 'source' => 'magento', 'events' => $events],
            JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_THROW_ON_ERROR
        );
        $headers = [
            'Content-Type' => 'application/json',
            'Accept' => 'application/json',
            'User-Agent' => self::USER_AGENT,
            'X-SDLC-Signature' => 'sha256=' . hash_hmac('sha256', $body, $secret),
            'X-SDLC-Timestamp' => (string)$timestamp,
            'X-SDLC-Event-Id' => $deliveryId,
        ];
        $database = $this->config->getOdooDatabase();
        if ($database !== '') {
            $headers['X-Odoo-Database'] = $database;
        }
        /** @var Curl $curl */
        $curl = $this->curlFactory->create();
        $timeout = $this->config->getTimeout();
        $curl->setTimeout($timeout);
        $curl->setOption(CURLOPT_CONNECTTIMEOUT, min($timeout, 5));
        $curl->setOption(CURLOPT_FOLLOWLOCATION, false);
        $curl->setOption(CURLOPT_PROTOCOLS, CURLPROTO_HTTP | CURLPROTO_HTTPS);
        if (!$this->config->verifySsl()) {
            $curl->setOption(CURLOPT_SSL_VERIFYPEER, false);
            $curl->setOption(CURLOPT_SSL_VERIFYHOST, 0);
        }
        $curl->setHeaders($headers);
        try {
            $curl->post($this->config->getEndpointUrl(), $body);
        } catch (\Throwable $e) {
            return ['ok' => false, 'status' => null, 'error' => mb_substr('Connection: ' . $e->getMessage(), 0, 255)];
        }
        $status = (int)$curl->getStatus();
        if ($status >= 200 && $status < 300) {
            return ['ok' => true, 'status' => $status, 'error' => ''];
        }
        $answer = preg_replace('/\s+/', ' ', mb_substr((string)$curl->getBody(), 0, 200));
        return ['ok' => false, 'status' => $status ?: null, 'error' => mb_substr("HTTP $status: $answer", 0, 255)];
    }
}
