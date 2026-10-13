<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use Magento\CatalogInventory\Api\StockConfigurationInterface;
use Magento\Framework\Event\Observer;
use Magento\Framework\Event\ObserverInterface;
use Magento\Sales\Model\Order\Creditmemo;
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;
use SDLC\OdooConnector\Model\ReturnToStockContext;

/**
 * sales_order_creditmemo_save_after.
 *
 * Also reports which order items went back to stock: Magento does not keep that flag on the
 * stored credit memo, so Odoo cannot read it back through the REST API. Sources: the items'
 * back_to_stock flag (admin credit memo form, automatic return to stock) and the
 * return_to_stock_items argument of the refund services (REST refunds). Order item ids only.
 */
class CreditmemoSaveObserver implements ObserverInterface
{
    /**
     * @param Recorder $recorder
     * @param ReturnToStockContext $returnToStockContext
     * @param StockConfigurationInterface $stockConfiguration
     */
    public function __construct(
        private readonly Recorder $recorder,
        private readonly ReturnToStockContext $returnToStockContext,
        private readonly StockConfigurationInterface $stockConfiguration
    ) {
    }

    /**
     * @param Observer $observer
     * @return void
     */
    public function execute(Observer $observer): void
    {
        $creditmemo = $observer->getEvent()->getData('creditmemo');
        if (!$creditmemo instanceof Creditmemo || !$creditmemo->getId() || !$creditmemo->getOrderId()) {
            return;
        }
        $returned = [];
        try {
            $returned = $this->returnToStockContext->get();
            $autoReturn = $this->stockConfiguration->isAutoReturnEnabled();
            foreach ($creditmemo->getAllItems() as $item) {
                if ((float)$item->getQty() > 0 && $item->getOrderItemId()
                    && ($autoReturn || $item->getBackToStock())) {
                    $returned[] = (int)$item->getOrderItemId();
                }
            }
        } catch (\Throwable $e) {
            $returned = [];
        }
        $returned = array_values(array_unique(array_filter(array_map('intval', $returned))));
        $this->recorder->record(
            Outbox::ENTITY_CREDITMEMO,
            $creditmemo->getId(),
            (string)$creditmemo->getIncrementId(),
            $creditmemo->getOrigData('entity_id') === null ? 'created' : 'updated',
            $creditmemo->getOrderId(),
            $returned
        );
    }
}
