<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Console\Command;

use SDLC\OdooConnector\Model\Sender;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;

/**
 * bin/magento sdlc:odoo:outbox:flush — send due outbox rows now (same code as the cron).
 */
class FlushCommand extends Command
{
    /**
     * @param Sender $sender
     */
    public function __construct(private readonly Sender $sender)
    {
        parent::__construct();
    }

    /**
     * @inheritdoc
     */
    protected function configure(): void
    {
        $this->setName('sdlc:odoo:outbox:flush')
            ->setDescription('Send the due Odoo Connector outbox events to Odoo now')
            ->addOption('batches', null, InputOption::VALUE_REQUIRED, 'Maximum number of requests', '20');
        parent::configure();
    }

    /**
     * @inheritdoc
     */
    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $stats = $this->sender->run(max(1, (int)$input->getOption('batches')));
        foreach ($stats as $key => $value) {
            $output->writeln(sprintf('%-14s %s', $key, $value === null ? '-' : (string)$value));
        }
        return $stats['rows_failed'] > 0 ? Command::FAILURE : Command::SUCCESS;
    }
}
