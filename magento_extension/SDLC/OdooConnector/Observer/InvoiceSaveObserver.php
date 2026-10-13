<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Observer;

use SDLC\OdooConnector\Model\Outbox;

/**
 * sales_order_invoice_save_after.
 */
class InvoiceSaveObserver extends AbstractDocumentObserver
{
    protected const DATA_KEY = 'invoice';
    protected const ENTITY = Outbox::ENTITY_INVOICE;
}
