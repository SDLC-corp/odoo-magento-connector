<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use Magento\Catalog\Model\Product;
use Magento\Framework\Event\Observer;
use Magento\Framework\Event\ObserverInterface;
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;

/**
 * catalog_product_save_after.
 */
class ProductSaveObserver implements ObserverInterface
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
        $product = $observer->getEvent()->getData('product');
        if (!$product instanceof Product || !$product->getId() || (string)$product->getSku() === '') {
            return;
        }
        $this->recorder->record(
            Outbox::ENTITY_PRODUCT,
            $product->getId(),
            (string)$product->getSku(),
            $product->getOrigData('entity_id') === null ? 'created' : 'updated'
        );
    }
}
