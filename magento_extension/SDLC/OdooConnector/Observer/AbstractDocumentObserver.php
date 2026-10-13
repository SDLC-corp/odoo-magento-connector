<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use Magento\Framework\Event\Observer;
use Magento\Framework\Event\ObserverInterface;
use Magento\Framework\Model\AbstractModel;
use SDLC\OdooConnector\Model\Recorder;

/**
 * Invoice, shipment or credit memo saved: Odoo re-imports the parent order with its documents.
 */
abstract class AbstractDocumentObserver implements ObserverInterface
{
    /** Event data key of the document (invoice, shipment, creditmemo). */
    protected const DATA_KEY = '';
    /** Outbox entity type. */
    protected const ENTITY = '';

    /**
     * @param Recorder $recorder
     */
    public function __construct(private readonly Recorder $recorder)
    {
    }

    /**
     * @param Observer $observer
     * @return void
     */
    public function execute(Observer $observer): void
    {
        $document = $observer->getEvent()->getData(static::DATA_KEY);
        if (!$document instanceof AbstractModel || !$document->getId() || !$document->getData('order_id')) {
            return;
        }
        $this->recorder->record(
            static::ENTITY,
            $document->getId(),
            (string)$document->getData('increment_id'),
            $document->getOrigData('entity_id') === null ? 'created' : 'updated',
            $document->getData('order_id')
        );
    }
}
