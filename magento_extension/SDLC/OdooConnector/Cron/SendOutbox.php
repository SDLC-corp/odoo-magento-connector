<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Cron;

use Psr\Log\LoggerInterface;
use SDLC\OdooConnector\Model\Sender;

/**
 * Every minute (cron group sdlc_odoo_connector): push due outbox rows to Odoo.
 */
class SendOutbox
{
    /**
     * @param Sender $sender
     * @param LoggerInterface $logger
     */
    public function __construct(
        private readonly Sender $sender,
        private readonly LoggerInterface $logger
    ) {
    }

    /**
     * @return void
     */
    public function execute(): void
    {
        try {
            $stats = $this->sender->run();
            if ($stats['batches'] > 0) {
                $this->logger->info('SDLC_OdooConnector: outbox run', $stats);
            }
        } catch (\Throwable $e) {
            $this->logger->error('SDLC_OdooConnector: outbox run failed: ' . $e->getMessage());
        }
    }
}
