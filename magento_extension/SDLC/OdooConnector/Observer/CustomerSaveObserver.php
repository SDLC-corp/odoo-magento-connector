<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use Magento\Customer\Model\Customer;
use Magento\Framework\Event\Observer;
use Magento\Framework\Event\ObserverInterface;
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;

/**
 * customer_save_after: only the customer id is sent (no name, email or address).
 */
class CustomerSaveObserver implements ObserverInterface
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
        $customer = $observer->getEvent()->getData('customer');
        if (!$customer instanceof Customer || !$customer->getId()) {
            return;
        }
        $this->recorder->record(
            Outbox::ENTITY_CUSTOMER,
            $customer->getId(),
            null,
            $customer->getOrigData('entity_id') === null ? 'created' : 'updated'
        );
    }
}
