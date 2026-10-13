<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Console\Command;

use SDLC\OdooConnector\Model\Outbox;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Helper\Table;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

/**
 * bin/magento sdlc:odoo:outbox:status — rows per status and the latest delivery problems.
 */
class StatusCommand extends Command
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
        $this->setName('sdlc:odoo:outbox:status')
            ->setDescription('Show the Odoo Connector outbox (rows per status, latest failures)');
        parent::configure();
    }

    /**
     * @inheritdoc
     */
    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $counts = $this->outbox->countByStatus();
        $output->writeln('Outbox rows per status:');
        foreach ([Outbox::STATUS_PENDING, Outbox::STATUS_PROCESSING, Outbox::STATUS_RETRY,
                     Outbox::STATUS_SENT, Outbox::STATUS_FAILED] as $status) {
            $output->writeln(sprintf('  %-10s %d', $status, $counts[$status] ?? 0));
        }
        $problems = $this->outbox->latestProblems();
        if ($problems) {
            $output->writeln('Latest retry / failed rows:');
            $table = new Table($output);
            $table->setHeaders(array_keys($problems[0]));
            $table->setRows($problems);
            $table->render();
        }
        return Command::SUCCESS;
    }
}
