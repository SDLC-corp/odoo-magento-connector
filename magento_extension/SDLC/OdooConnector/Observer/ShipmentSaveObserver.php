<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use SDLC\OdooConnector\Model\Outbox;

/**
 * sales_order_shipment_save_after.
 */
class ShipmentSaveObserver extends AbstractDocumentObserver
{
    protected const DATA_KEY = 'shipment';
    protected const ENTITY = Outbox::ENTITY_SHIPMENT;
}
