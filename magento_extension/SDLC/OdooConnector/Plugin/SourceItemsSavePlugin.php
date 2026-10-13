<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Plugin;

use Magento\InventoryApi\Api\SourceItemsSaveInterface;
use SDLC\OdooConnector\Model\Outbox;
use SDLC\OdooConnector\Model\Recorder;

/**
 * Multi-Source Inventory: a saved source item is a stock event for its SKU.
 */
class SourceItemsSavePlugin
{
    /**
     * @param Recorder $recorder
     */
    public function __construct(private readonly Recorder $recorder)
    {
    }

    /**
     * @param SourceItemsSaveInterface $subject
     * @param mixed $result
     * @param array $sourceItems
     * @return mixed
     * @SuppressWarnings(PHPMD.UnusedFormalParameter)
     */
    public function afterExecute(SourceItemsSaveInterface $subject, mixed $result, array $sourceItems): mixed
    {
        $skus = [];
        foreach ($sourceItems as $sourceItem) {
            $sku = method_exists($sourceItem, 'getSku') ? (string)$sourceItem->getSku() : '';
            if ($sku !== '') {
                $skus[$sku] = true;
            }
        }
        foreach (array_keys($skus) as $sku) {
            $this->recorder->record(Outbox::ENTITY_STOCK, null, (string)$sku, 'updated');
        }
        return $result;
    }
}
