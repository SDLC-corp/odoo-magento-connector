<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Cron;

use Psr\Log\LoggerInterface;
use SDLC\OdooConnector\Model\Config;
use SDLC\OdooConnector\Model\Outbox;

/**
 * Daily: delete delivered rows after the retention period and failed rows after 30 days.
 */
class CleanOutbox
{
    /**
     * @param Outbox $outbox
     * @param Config $config
     * @param LoggerInterface $logger
     */
    public function __construct(
        private readonly Outbox $outbox,
        private readonly Config $config,
        private readonly LoggerInterface $logger
    ) {
    }

    /**
     * @return void
     */
    public function execute(): void
    {
        try {
            $deleted = $this->outbox->cleanup($this->config->getRetentionDays());
            if ($deleted) {
                $this->logger->info(sprintf('SDLC_OdooConnector: %d old outbox rows deleted', $deleted));
            }
        } catch (\Throwable $e) {
            $this->logger->error('SDLC_OdooConnector: outbox cleanup failed: ' . $e->getMessage());
        }
    }
}
