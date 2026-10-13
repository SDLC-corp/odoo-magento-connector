<?php
/**
 * @copyright SDLC Corp
 * @license   MIT
 */
declare(strict_types=1);

namespace SDLC\OdooConnector\Plugin;

use Magento\Sales\Api\Data\CreditmemoCommentCreationInterface;
use Magento\Sales\Api\Data\CreditmemoCreationArgumentsInterface;
use Magento\Sales\Api\RefundOrderInterface;
use SDLC\OdooConnector\Model\ReturnToStockContext;

/**
 * POST /V1/order/{id}/refund: remember arguments.extension_attributes.return_to_stock_items
 * while the credit memo is saved.
 */
class RefundOrderReturnToStockPlugin
{
    /**
     * @param ReturnToStockContext $context
     */
    public function __construct(private readonly ReturnToStockContext $context)
    {
    }

    /**
     * @param RefundOrderInterface $subject
     * @param callable $proceed
     * @param int $orderId
     * @param array $items
     * @param bool $notify
     * @param bool $appendComment
     * @param CreditmemoCommentCreationInterface|null $comment
     * @param CreditmemoCreationArgumentsInterface|null $arguments
     * @return mixed
     * @SuppressWarnings(PHPMD.UnusedFormalParameter)
     * @SuppressWarnings(PHPMD.BooleanArgumentFlag)
     */
    public function aroundExecute(
        RefundOrderInterface $subject,
        callable $proceed,
        $orderId,
        array $items = [],
        $notify = false,
        $appendComment = false,
        ?CreditmemoCommentCreationInterface $comment = null,
        ?CreditmemoCreationArgumentsInterface $arguments = null
    ) {
        $this->context->set(ReturnToStockArguments::itemIds($arguments));
        try {
            return $proceed($orderId, $items, $notify, $appendComment, $comment, $arguments);
        } finally {
            $this->context->clear();
        }
    }
}
