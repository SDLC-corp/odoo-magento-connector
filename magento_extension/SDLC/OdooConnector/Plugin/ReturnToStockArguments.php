<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Plugin;

use Magento\Sales\Api\Data\CreditmemoCreationArgumentsInterface;

/**
 * Reads `extension_attributes.return_to_stock_items` (Magento_SalesInventory) safely.
 */
class ReturnToStockArguments
{
    /**
     * @param CreditmemoCreationArgumentsInterface|null $arguments
     * @return int[]|null
     */
    public static function itemIds(?CreditmemoCreationArgumentsInterface $arguments): ?array
    {
        if ($arguments === null) {
            return null;
        }
        $extension = $arguments->getExtensionAttributes();
        if ($extension === null || !method_exists($extension, 'getReturnToStockItems')) {
            return null;
        }
        $ids = $extension->getReturnToStockItems();
        return is_array($ids) ? array_map('intval', $ids) : null;
    }
}
