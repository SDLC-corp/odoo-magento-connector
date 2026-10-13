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
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;

/**
 * cataloginventory_stock_item_save_after (legacy stock item; MSI source items are handled by
 * the SourceItemsSavePlugin). The SKU is looked up after commit.
 */
class StockItemSaveObserver implements ObserverInterface
{
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
        $item = $observer->getEvent()->getData('item');
        if (!$item instanceof AbstractModel || !$item->getData('product_id')) {
            return;
        }
        $sku = $item->getData('sku');
        $this->recorder->record(
            Outbox::ENTITY_STOCK,
            $item->getData('product_id'),
            is_string($sku) && $sku !== '' ? $sku : null,
            'updated'
        );
    }
}
