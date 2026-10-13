<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use Magento\Framework\Event\Observer;
use Magento\Framework\Event\ObserverInterface;
use Magento\Sales\Model\Order;
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;

/**
 * sales_order_save_after: order placed, updated or status changed.
 */
class OrderSaveObserver implements ObserverInterface
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
        $order = $observer->getEvent()->getData('order');
        if (!$order instanceof Order || !$order->getId()) {
            return;
        }
        if ($order->getOrigData('entity_id') === null) {
            $type = 'created';
        } elseif ($order->dataHasChangedFor('status') || $order->dataHasChangedFor('state')) {
            $type = 'status_changed';
        } else {
            $type = 'updated';
        }
        $this->recorder->record(
            Outbox::ENTITY_ORDER,
            $order->getId(),
            (string)$order->getIncrementId(),
            $type
        );
    }
}
