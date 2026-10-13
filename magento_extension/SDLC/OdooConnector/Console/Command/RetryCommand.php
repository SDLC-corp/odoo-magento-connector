<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Console\Command;

use SDLC\OdooConnector\Model\Outbox;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

/**
 * bin/magento sdlc:odoo:outbox:retry — make failed and backing-off rows due now
 * (e.g. after fixing the URL or secret).
 */
class RetryCommand extends Command
{
    /**
     * @param Outbox $outbox
     */
    public function __construct(private readonly Outbox $outbox)
    {
        parent::__construct();
    }

    /**
     * @inheritdoc
     */
    protected function configure(): void
    {
        $this->setName('sdlc:odoo:outbox:retry')
            ->setDescription('Requeue failed and backing-off Odoo Connector outbox rows (attempts reset)');
        parent::configure();
    }

    /**
     * @inheritdoc
     */
    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $output->writeln(sprintf('%d rows requeued.', $this->outbox->retryFailed()));
        return Command::SUCCESS;
    }
}
